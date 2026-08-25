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
    from services.sales_case_service import apply_sales_decision, get_sales_context

    try:
        current_sales_context = await get_sales_context(org_id, conversation_id)
    except Exception as exc:
        logger.warning(f"Could not load sales context for {conversation_id}: {exc}")
        current_sales_context = {"version": 0, "stage": "new", "facts": {}}

    # Prepare the request payload
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
            
            # Extract the chatbot's response
            final_response = validate_ai_reply(response_data.get("response"))
            logger.info(f"Received response from chatbot for ...{conversation_id[-6:]}")
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
                    sales_event_id = message_id or f"agent_{uuid.uuid4()}"
                    applied = await apply_sales_decision(
                        org_id=org_id,
                        conversation_id=conversation_id,
                        source_message_id=sales_event_id,
                        customer_name=customer_name,
                        decision=sales_decision,
                        source_message_text=user_input,
                    )
                    if applied.get("duplicate"):
                        result["duplicate_event"] = True
                        result["sales_case"] = await get_sales_context(org_id, conversation_id)
                        return result
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
        
    except httpx.HTTPStatusError as e:
        raise AIProviderUnavailable("http_error", upstream_status=e.response.status_code) from e
    except httpx.RequestError as exc:
        raise AIProviderUnavailable("network_error") from exc
        
    except Exception as e:
        raise AIProviderUnavailable("unexpected_error") from e
    
