import asyncio
import hashlib
import os
from urllib.parse import urlsplit
import re
import time

import httpx
import uuid
from typing import Dict, Optional
from loguru import logger
from config.settings import CHATBOT_BASE_URL, INTERNAL_HMAC_SECRET
from services.ai_reply_guard import AIProviderUnavailable, validate_ai_reply
from services.internal_request import signed_json

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


def _record_learning_later(work) -> None:
    """Optional analytics must not hold a customer reply behind database writes."""
    from core.services import services
    async def record():
        try:
            await work
        except Exception as exc:
            logger.warning(f"Could not record internal sales learning: {exc}")
    # ponytail: best effort analytics can be lost on process exit; use the
    # existing durable event worker if learning becomes delivery-critical.
    task = asyncio.create_task(record())
    services.background_tasks.add(task)
    task.add_done_callback(services.background_tasks.discard)


def _chatbot_endpoint(org_id: str) -> str:
    """Opt one organization into a test endpoint; keep normal routing for others."""
    target_org = os.getenv("CHATBOT_TEST_ORG_ID", "").strip()
    if not target_org or org_id != target_org:
        return f"{CHATBOT_BASE_URL}/api/v2/chat"
    base = os.getenv("CHATBOT_TEST_BASE_URL", "").strip().rstrip("/")
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and not parsed.path and not any(char.isspace() for char in base))
        parsed.port  # Validate a supplied port without exposing the configured URL.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("CHATBOT_TEST_BASE_URL must be an HTTPS origin without credentials, path, query or fragment")
    return f"{base}/api/v2/chat"


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
    result: Dict[str, object] = {"requested": True, "reason_code": reason}
    if "context" in value:
        context = value.get("context")
        # Optional and catalogue-derived only (product/price) - never the
        # customer's own message. A malformed value here fails the whole
        # handoff closed, same as a malformed reason_code.
        if not isinstance(context, str) or len(context) > _HANDOFF_CONTEXT_MAX_CHARS:
            raise AIProviderUnavailable("malformed_handoff")
        context = context.strip()
        if context:
            result["context"] = context
    return result


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
    context = handoff.get("context")
    # Shown to the reviewer right alongside the customer's own message - a
    # bare "any discount on this?" told a reviewer nothing about what "this"
    # was without opening the full chat.
    escalation_text = f"{customer_text}\n\n{context}" if context else customer_text
    metadata: Dict[str, object] = {
        "reason": handoff["reason_code"],
        "conversation_id": conversation_id,
        "source_message_id": message_id,
        "platform": platform,
        "source": source,
    }
    if context:
        metadata["context"] = context
    try:
        saved = await create_ai_escalation(
            org_id,
            {
                "id": escalation_id,
                "dedupe_id": escalation_id,
                "user_name": customer_name,
                "customer_number": conversation_id,
                "customer_text": escalation_text,
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
    from services.sales_workflow_service import automatic_purchase
    purchase_reply = await automatic_purchase(org_id, conversation_id, sales_event_id, user_input)
    if purchase_reply is not None:
        return purchase_reply

    try:
        active_sales_case = await get_active_sales_case(org_id, conversation_id)
        current_sales_context = sales_context(active_sales_case)
    except Exception as exc:
        logger.warning(f"Could not load optional sales context for {conversation_id}: {exc}")
        active_sales_case = None
        current_sales_context = sales_context(None)

    recovery_receipt = next((
        item for item in reversed((active_sales_case or {}).get("action_receipts") or [])
        if isinstance(item, dict) and item.get("source_message_id") == sales_event_id
    ), None)
    legacy_action_type = ""
    if (
        active_sales_case
        and active_sales_case.get("action_source_message_id") == sales_event_id
        and active_sales_case.get("action_status") in {
            "pending", "processing", "ready", "completed", "blocked", "failed",
        }
    ):
        legacy_action_type = str(
            active_sales_case.get("action_type")
            or active_sales_case.get("next_action")
            or ""
        )
    recovery_action_type = str(
        (recovery_receipt or {}).get("action_type") or legacy_action_type
    )
    if recovery_action_type and recovery_action_type not in {"none", "show_products"}:
        try:
            from services.sales_action_service import execute_sales_action

            recovery_case = (recovery_receipt or {}).get("case_snapshot")
            has_recovery_snapshot = isinstance(recovery_case, dict)
            if not has_recovery_snapshot:
                recovery_case = active_sales_case
            action = await execute_sales_action(
                case=recovery_case,
                source_message_id=sales_event_id,
                action_type=recovery_action_type,
                customer_text=user_input,
                recover_result_only=not has_recovery_snapshot,
            )
            if action and action.get("requires_review"):
                raise AIProviderUnavailable(
                    action.get("review_category")
                    or "legacy_sales_action_requires_review"
                )
            if action and action.get("pending"):
                raise AIProviderUnavailable(
                    "sales_action_in_progress", escalation_required=False
                )
            if action:
                return {
                    "final_response": action["reply"],
                    "usage": {},
                    "sales_action": action,
                    "sales_case": await get_sales_context(org_id, conversation_id),
                    "sales_case_conflict": False,
                    "sales_case_rebased": False,
                    "product_timings": {
                        "math_ms": 0,
                        "agent_total_ms": round(
                            (time.perf_counter() - request_started) * 1000
                        ),
                    },
                }
        except AIProviderUnavailable:
            raise
        except Exception as exc:
            raise AIProviderUnavailable("unexpected_error") from exc

    from services.commerce_knowledge import buying_answer
    buying_result = await buying_answer(org_id, user_input, conversation_id, message_id, customer_name, source)
    if buying_result is not None:
        return buying_result

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

    if active_sales_case:
        _record_learning_later(record_existing_customer_response())
    customer_response_recorded = active_sales_case is not None
    try:
        sales_guidance = await get_sales_guidance(org_id, conversation_id, current_sales_context, user_input)
    except Exception as exc:
        logger.warning(f"Could not load internal sales guidance for {conversation_id}: {exc}")
        sales_guidance = {"policy_version": "default", "cohort": "control", "recommended_moves": []}

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

    endpoint = _chatbot_endpoint(org_id)
    body, headers = signed_json(payload, "/api/v2/chat", INTERNAL_HMAC_SECRET, message_id)

    try:
        logger.info(f"Sending message to chatbot for ...{conversation_id[-6:]}")
        logger.debug(
            f"Chatbot payload org={org_id}, conversation=...{conversation_id[-4:]}, "
            f"input_length={len(user_input or '')}"
        )
        
        math_started = time.perf_counter()
        response = await _chatbot_http_client().post(
            endpoint,
            content=body,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        math_ms = round((time.perf_counter() - math_started) * 1000)

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
            "product_timings": {"math_ms": math_ms},
        }
        reply_to_message_id = response_data.get("reply_to_message_id")
        if reply_to_message_id:
            result["reply_to_message_id"] = reply_to_message_id
        response_class = response_data.get("response_class")
        if response_class in {
            "product_match", "product_alternative", "oos", "discount", "other",
        }:
            result["response_class"] = response_class
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
                if not applied.get("duplicate"):
                    async def record_decision():
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
                    _record_learning_later(record_decision())
                receipt = applied.get("action_receipt") or {}
                action_type = str(receipt.get("action_type") or "none")
                action_source_message_id = str(
                    receipt.get("source_message_id") or sales_event_id
                )
                if (
                    applied.get("duplicate")
                    and not receipt
                    and applied["case"].get("action_source_message_id") == sales_event_id
                ):
                    action_type = str(
                        applied["case"].get("action_type")
                        or applied["case"].get("next_action")
                        or "none"
                    )
                from services.sales_action_service import execute_sales_action

                action_case = receipt.get("case_snapshot")
                if not isinstance(action_case, dict):
                    action_case = applied["case"]
                action = None if applied.get("conflict") else await execute_sales_action(
                    case=action_case,
                    source_message_id=action_source_message_id,
                    action_type=action_type,
                    customer_text=user_input,
                )
                if action and action.get("requires_review"):
                    raise AIProviderUnavailable(
                        action.get("review_category")
                        or "sales_action_review_required"
                    )
                if action and action.get("pending"):
                    raise AIProviderUnavailable(
                        "sales_action_in_progress", escalation_required=False
                    )
                if action:
                    result["final_response"] = action["reply"]
                    result["sales_action"] = action
                result["sales_case"] = await get_sales_context(org_id, conversation_id)
                result["sales_case_conflict"] = applied["conflict"]
                result["sales_case_rebased"] = applied.get("rebased", False)
            except AIProviderUnavailable:
                raise
            except Exception as exc:
                logger.error(
                    f"Could not save sales decision for {conversation_id}: {exc}",
                    exc_info=True,
                )
                result["final_response"] = (
                    "I could not save that sales step safely. Please try again."
                )
        result["product_timings"]["agent_total_ms"] = round(
            (time.perf_counter() - request_started) * 1000
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
