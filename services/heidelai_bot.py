import httpx
from typing import Dict, Any, Optional
from loguru import logger
from services.ai_reply_guard import AIProviderUnavailable, validate_ai_reply


# Base URL for the chatbot API
CHATBOT_BASE_URL = "https://heidelai-backend2.koyeb.app"

async def send_to_chatbot(
    customer_phone_no: str,
    message_content: str,
    timeout: float = 30.0,
    message_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Send a message to the HeidelAI chatbot and get a response.

    Args:
        customer_phone_no: Customer's phone number (will be prefixed with 'whatsapp_')
        message_content: The message content from the customer
        timeout: Request timeout in seconds (default: 30s)
        message_id: Optional WhatsApp wamid of the incoming message (forwarded to the backend)

    Returns:
        A dict containing at least ``"response"`` on success, optionally
        ``"reply_to_message_id"``.
    """
    # Prepare the identifier with whatsapp_ prefix
    identifier = f"whatsapp_{customer_phone_no}"
    
    # Prepare the request payload
    payload = {
        "identifier": identifier,
        "query": message_content
    }
    if message_id:
        payload["message_id"] = message_id
    
    # API endpoint
    endpoint = f"{CHATBOT_BASE_URL}/chat"
    
    try:
        logger.info(f"Sending message to chatbot for ...{identifier[-6:]}")
        logger.debug(
            f"HeidelAI chatbot payload conversation=...{identifier[-4:]}, "
            f"input_length={len(message_content or '')}"
        )
        
        # Make async HTTP request
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"}
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
            response_data["response"] = validate_ai_reply(response_data.get("response"))
            logger.info(f"Received response from chatbot for ...{identifier[-6:]}")
            return response_data
                
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


def send_to_chatbot_sync(
    customer_phone_no: str,
    message_content: str,
    timeout: float = 30.0,
    message_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Synchronous version of send_to_chatbot for use in non-async contexts.

    Args:
        customer_phone_no: Customer's phone number (will be prefixed with 'whatsapp_')
        message_content: The message content from the customer
        timeout: Request timeout in seconds (default: 30s)
        message_id: Optional WhatsApp wamid of the incoming message (forwarded to the backend)

    Returns:
        A dict containing at least ``"response"`` on success, optionally
        ``"reply_to_message_id"``.
    """
    import requests
    
    # Prepare the identifier with whatsapp_ prefix
    identifier = f"whatsapp_{customer_phone_no}"
    
    # Prepare the request payload
    payload = {
        "identifier": identifier,
        "query": message_content
    }
    if message_id:
        payload["message_id"] = message_id
    
    # API endpoint
    endpoint = f"{CHATBOT_BASE_URL}/chat"
    
    try:
        logger.info(f"Sending message to chatbot for ...{identifier[-6:]}")
        logger.debug(
            f"HeidelAI chatbot payload conversation=...{identifier[-4:]}, "
            f"input_length={len(message_content or '')}"
        )
        
        # Make synchronous HTTP request
        response = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout
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
        response_data["response"] = validate_ai_reply(response_data.get("response"))
        logger.info(f"Received response from chatbot for ...{identifier[-6:]}")
        return response_data
            
    except AIProviderUnavailable:
        raise
    except requests.Timeout as exc:
        raise AIProviderUnavailable("timeout") from exc

    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        raise AIProviderUnavailable("http_error", upstream_status=status) from e
    except requests.RequestException as exc:
        raise AIProviderUnavailable("network_error") from exc

    except Exception as e:
        raise AIProviderUnavailable("unexpected_error") from e

if __name__ == "__main__":
    # Example usage
    import asyncio
    
    async def test_chatbot():
        response = await send_to_chatbot("919511907785", "Hello, how are you?")
        print("Chatbot response:", response)
    
    asyncio.run(test_chatbot())
