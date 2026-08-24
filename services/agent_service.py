import httpx
import json
import uuid
import time
import base64
import hmac
import hashlib
from typing import Dict, Optional
from loguru import logger
from config.settings import X_INTERNAL_SECRET
from services.ai_reply_guard import AIProviderUnavailable, validate_ai_reply

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


    if not X_INTERNAL_SECRET:
        raise AIProviderUnavailable("configuration_error")
    
    # Generate request signature headers for v2 chat API
    request_id = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    body_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    canonical = f"POST\n/api/v2/chat\n{timestamp}\n{request_id}\n".encode() + body_bytes
    signature = base64.b64encode(hmac.new(X_INTERNAL_SECRET.encode(), canonical, hashlib.sha256).digest()).decode()

    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": X_INTERNAL_SECRET,
        "X-Request-Id": request_id,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }
    
    # API endpoint
    endpoint = f"{CHATBOT_BASE_URL}/api/v2/chat"
    
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
                json=payload,
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
    
