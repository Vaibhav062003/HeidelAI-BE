import asyncio
import hashlib
import re
import time

import httpx
import uuid
from typing import Dict, Optional
from loguru import logger
from config.settings import INTERNAL_HMAC_SECRET
from services.ai_reply_guard import AIProviderUnavailable, validate_ai_reply
from services.internal_request import signed_json

# Base URL for the chatbot API
CHATBOT_BASE_URL = "https://backend-chatbot.koyeb.app"
#CHATBOT_BASE_URL = "http://localhost:8001"  For local testing with self-signed certs, ensure to disable SSL verification in httpx client

_HANDOFF_REASON = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_HANDOFF_REASONS = {
    "after_sales",
    "billing",
    "bulk_purchase",
    "dealer_query",
    "discount",
    "human",
    "human_review",
    "missing_sku_model_lookup",
    "order_placement",
    "pricing",
    "quotation",
    "support",
}
_HANDOFF_KEYS = {"requested", "reason_code", "context"}
_HANDOFF_CONTEXT_MAX_CHARS = 500
_chatbot_client: Optional[httpx.AsyncClient] = None


def _chatbot_http_client() -> httpx.AsyncClient:
    """Reuse transport connections; responses and catalogue data are not cached."""
    global _chatbot_client
    if _chatbot_client is None or getattr(_chatbot_client, "is_closed", False):
        _chatbot_client = httpx.AsyncClient()
    return _chatbot_client


async def close_agent_http_client() -> None:
    global _chatbot_client
    if _chatbot_client is not None and not getattr(_chatbot_client, "is_closed", False):
        await _chatbot_client.aclose()
    _chatbot_client = None


def _validated_handoff(value: object) -> Optional[Dict[str, object]]:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or not set(value) <= _HANDOFF_KEYS
        or "reason_code" not in value
        or value.get("requested") is not True
    ):
        raise AIProviderUnavailable("malformed_handoff")
    reason = value.get("reason_code")
    if (
        not isinstance(reason, str)
        or not 1 <= len(reason) <= 64
        or not _HANDOFF_REASON.fullmatch(reason)
        or reason not in _HANDOFF_REASONS
    ):
        raise AIProviderUnavailable("malformed_handoff")
    handoff: Dict[str, object] = {"requested": True, "reason_code": reason}
    if "context" in value:
        context = value.get("context")
        if context is not None and not isinstance(context, str):
            raise AIProviderUnavailable("malformed_handoff")
        if isinstance(context, str):
            context = context.strip()
            if len(context) > _HANDOFF_CONTEXT_MAX_CHARS:
                raise AIProviderUnavailable("malformed_handoff")
            if context:
                handoff["context"] = context
    return handoff


async def _persist_handoff(
    handoff: Dict[str, object],
    *,
    org_id: str,
    conversation_id: str,
    message_id: Optional[str],
    customer_name: str,
    customer_text: str,
    source: str,
) -> None:
    if not message_id:
        raise AIProviderUnavailable("handoff_missing_message_id")

    from services.team_service import create_ai_escalation

    escalation_id = "ai-handoff-" + hashlib.sha256(
        f"{org_id}\0{message_id}".encode("utf-8")
    ).hexdigest()[:32]
    platform = conversation_id.partition("_")[0].lower() or "unknown"
    metadata: Dict[str, object] = {
        "reason": handoff["reason_code"],
        "conversation_id": conversation_id,
        "source_message_id": message_id,
        "platform": platform,
        "source": source,
    }
    if handoff.get("context"):
        metadata["catalogue_context"] = handoff["context"]
    try:
        saved = await create_ai_escalation(
            org_id,
            {
                "id": escalation_id,
                "dedupe_id": escalation_id,
                "user_name": customer_name,
                "customer_number": conversation_id,
                "customer_text": customer_text,
                "inferred_intent": handoff["reason_code"],
                "priority": "high",
                "kind": "actionable",
                "status": "pending_human_review",
                "sender_name": "AI Assistant",
                "metadata": metadata,
            },
        )
    except AIProviderUnavailable:
        raise
    except Exception as exc:
        raise AIProviderUnavailable("handoff_persistence_failed") from exc
    if not isinstance(saved, dict) or saved.get("id") != escalation_id:
        raise AIProviderUnavailable("handoff_persistence_failed")


def _reported_usage(value) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("input_tokens", "output_tokens", "recorded_cost_microusd"):
        amount = value.get(key)
        if isinstance(amount, int) and not isinstance(amount, bool) and 0 <= amount <= 10**12:
            result[key] = amount
    return result


async def send_to_agent(
    conversation_id: str,
    user_input: str,
    persona: str,
    location: str,
    customer_name: str,
    org_id: str,
    source: str = "webhook",
    timeout: int = 30,
    reply_context_message_id: str = None,
    message_id: str = None,
    version: str = None,
    message_type: str = "text",
) -> Dict:
    """Send a signed message to Math and apply its checked sales result."""
    request_started = time.perf_counter()
    from services.sales_case_service import (
        apply_sales_decision,
        get_active_sales_case,
        get_sales_context,
        sales_context,
    )
    sales_event_id = message_id or f"agent_{uuid.uuid4()}"

    try:
        active_sales_case = await get_active_sales_case(org_id, conversation_id)
    except Exception as exc:
        logger.warning(f"Could not load optional sales context for {conversation_id}: {exc}")
        active_sales_case = None
        current_sales_context = {}
    else:
        current_sales_context = sales_context(active_sales_case)

    from services.sales_learning_service import get_sales_guidance, record_customer_response
    channel = "instagram" if conversation_id.startswith("instagram_") else "whatsapp"

    async def record_existing_customer_response() -> None:
        if active_sales_case:
            await record_customer_response(
                org_id, conversation_id, sales_event_id, user_input,
                target_message_id=reply_context_message_id or "",
                channel=channel,
                case=active_sales_case,
            )

    recorded, guidance = await asyncio.gather(
        record_existing_customer_response(),
        get_sales_guidance(org_id, conversation_id, current_sales_context, user_input),
        return_exceptions=True,
    )
    customer_response_recorded = active_sales_case is not None and not isinstance(recorded, BaseException)
    if isinstance(recorded, BaseException):
        logger.warning(f"Could not record customer response for {conversation_id}: {recorded}")
    if isinstance(guidance, BaseException):
        logger.warning(f"Could not load internal sales guidance for {conversation_id}: {guidance}")
        sales_guidance = {"policy_version": "default", "cohort": "control", "recommended_moves": []}
    else:
        sales_guidance = guidance

    payload = {
        "conversation_id": conversation_id,
        "user_input": user_input,
        "persona": persona,
        "location": location,
        "customer_name": customer_name,
        "org_id": org_id,
        "source": source,
        "message_type": message_type,
        "sales_context": current_sales_context,
        "sales_guidance": sales_guidance,
    }
    if message_id:
        payload["message_id"] = message_id
    if reply_context_message_id:
        payload["reply_context_message_id"] = reply_context_message_id

    endpoint = f"{CHATBOT_BASE_URL}/api/v2/chat"
    body, headers = signed_json(payload, "/api/v2/chat", INTERNAL_HMAC_SECRET, message_id)

    try:
        logger.info(f"Sending message to chatbot for ...{conversation_id[-6:]}")
        logger.debug(
            f"Chatbot payload org={org_id}, conversation=...{conversation_id[-4:]}, "
            f"input_length={len(user_input or '')}"
        )
        
        # Make async HTTP request
        response = await _chatbot_http_client().post(
            endpoint,
            content=body,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()

        try:
            response_data = response.json()
        except ValueError as exc:
            raise AIProviderUnavailable("malformed_response") from exc
        if not isinstance(response_data, dict):
            raise AIProviderUnavailable("malformed_response")
            
        handoff = _validated_handoff(response_data.get("handoff"))
        if handoff:
            await _persist_handoff(
                handoff,
                org_id=org_id,
                conversation_id=conversation_id,
                message_id=message_id,
                customer_name=customer_name,
                customer_text=user_input,
                source=source,
            )

        # The handoff must be durable before a customer-facing reply is accepted.
        try:
            final_response = validate_ai_reply(response_data.get("response"))
        except AIProviderUnavailable as exc:
            if handoff:
                raise AIProviderUnavailable(
                    exc.category,
                    upstream_status=exc.upstream_status,
                    escalation_required=False,
                ) from exc
            raise
        logger.info(
            "Received response from chatbot for ...{} in {}ms",
            conversation_id[-6:],
            round((time.perf_counter() - request_started) * 1000),
        )
        result = {
            "final_response": final_response,
            "usage": _reported_usage(response_data.get("usage")),
        }
        reply_to_message_id = response_data.get("reply_to_message_id")
        if reply_to_message_id:
            result["reply_to_message_id"] = reply_to_message_id
        sales_decision = response_data.get("sales_decision")
        if sales_decision:
            result["sales_decision"] = sales_decision
            try:
                applied = await apply_sales_decision(
                    org_id=org_id,
                    conversation_id=conversation_id,
                    source_message_id=sales_event_id,
                    customer_name=customer_name,
                    decision=sales_decision,
                    source_message_text=user_input,
                )
                if applied.get("duplicate"):
                    pending_action = str(applied["case"].get("next_action") or "none")
                    if applied["case"].get("action_status") == "ready" and pending_action != "none":
                        from services.sales_action_service import execute_sales_action

                        action = await execute_sales_action(
                            case=applied["case"], source_message_id=sales_event_id,
                            action_type=pending_action, customer_text=user_input,
                        )
                        if action:
                            result["final_response"] = action["reply"]
                            result["sales_action"] = action
                            result["sales_case"] = await get_sales_context(org_id, conversation_id)
                            return result
                    result["duplicate_event"] = True
                    result["sales_case"] = await get_sales_context(org_id, conversation_id)
                    return result

                try:
                    from services.sales_learning_service import record_sales_learning_event

                    if not customer_response_recorded:
                        await record_customer_response(
                            org_id, conversation_id, sales_event_id, user_input,
                            target_message_id=reply_context_message_id or "",
                            channel=channel,
                            case=applied["case"],
                        )
                    await record_sales_learning_event(
                        org_id=org_id, event_type="ai_decision",
                        conversation_id=conversation_id, source_id=sales_event_id,
                        case=applied["case"], channel=channel,
                    )
                    if applied["case"].get("stage") == "commitment_received":
                        await record_sales_learning_event(
                            org_id=org_id, event_type="commitment",
                            conversation_id=conversation_id,
                            source_id=f"{sales_event_id}:commitment",
                            case=applied["case"],
                        )
                except Exception as exc:
                    logger.warning(f"Could not record internal sales decision: {exc}")
                action_type = str(
                    (sales_decision.get("requested_action") or {}).get("type") or "none"
                )
                from services.sales_action_service import execute_sales_action

                action = None if applied["conflict"] else await execute_sales_action(
                    case=applied["case"],
                    source_message_id=sales_event_id,
                    action_type=action_type,
                    customer_text=user_input,
                )
                if action:
                    result["final_response"] = action["reply"]
                    result["sales_action"] = action
                result["sales_case"] = await get_sales_context(org_id, conversation_id)
                result["sales_case_conflict"] = applied["conflict"]
                result["sales_case_rebased"] = applied.get("rebased", False)
            except Exception as exc:
                logger.error(
                    f"Could not save sales decision for {conversation_id}: {exc}",
                    exc_info=True,
                )
                result["final_response"] = (
                    "I could not save that sales step safely. Please try again."
                )
        return result

    except AIProviderUnavailable:
        raise
    except httpx.TimeoutException as exc:
        raise AIProviderUnavailable("timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise AIProviderUnavailable(
            "http_error", upstream_status=exc.response.status_code
        ) from exc
    except httpx.RequestError as exc:
        raise AIProviderUnavailable("network_error") from exc
    except Exception as exc:
        raise AIProviderUnavailable("unexpected_error") from exc
