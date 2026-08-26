import hashlib
import re

import httpx
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


def _validated_handoff(value: object) -> Optional[Dict[str, object]]:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"requested", "reason_code"}
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
    return {"requested": True, "reason_code": reason}


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
                "metadata": {
                    "reason": handoff["reason_code"],
                    "conversation_id": conversation_id,
                    "source_message_id": message_id,
                    "platform": platform,
                    "source": source,
                },
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
    version: str = None
) -> Dict:

    """
    Send a message to the chatbot service and return the chatbot's response.
    Args:
        conversation_id: Unique identifier for the current conversation/session.
        user_input: The latest message or query from the user.
        persona: The chatbot persona or configuration to use.
        location: The user's location or relevant location context.
        customer_name: Name of the customer interacting with the chatbot.
        org_id: Organization identifier used for routing/authorization.
        source: Source of the message (for example, "webhook").
        timeout: Timeout in seconds for the HTTP request to the chatbot service.
    Returns:
        A dictionary containing the key ``"final_response"`` on success.
    """
    # Prepare the request payload
    payload = {
            "conversation_id": conversation_id,
            "user_input": user_input,
            "persona": persona,
            "location": location,
            "customer_name": customer_name,
            "org_id": org_id,
            "source": source
        }
    
    if message_id:
        payload["message_id"] = message_id

    
    if reply_context_message_id:
        payload["reply_context_message_id"] = reply_context_message_id


    # API endpoint
    endpoint = f"{CHATBOT_BASE_URL}/api/v2/chat"
    body, headers = signed_json(payload, "/api/v2/chat", INTERNAL_HMAC_SECRET, message_id)
    
    try:
        logger.info(f"Sending message to chatbot for ...{conversation_id[-6:]}")
        logger.debug(
            f"Chatbot payload org={org_id}, conversation=...{conversation_id[-4:]}, "
            f"input_length={len(user_input or '')}"
        )
        
        # Make async HTTP request
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint,
                content=body,
                headers=headers,
            )
            
            # Check if request was successful
            response.raise_for_status()
            
            # Parse response
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
            logger.info(f"Received response from chatbot for ...{conversation_id[-6:]}")
            return {"final_response": final_response, "usage": _reported_usage(response_data.get("usage"))}

    except AIProviderUnavailable:
        raise
    except httpx.TimeoutException as exc:
        raise AIProviderUnavailable("timeout") from exc
        
    except httpx.HTTPStatusError as e:
        raise AIProviderUnavailable("http_error", upstream_status=e.response.status_code) from e
    except httpx.RequestError as exc:
        raise AIProviderUnavailable("network_error") from exc
        
    except Exception as e:
        raise AIProviderUnavailable("unexpected_error") from e
    
