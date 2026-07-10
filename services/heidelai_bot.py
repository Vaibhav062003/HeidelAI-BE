import httpx
from typing import Dict, Any, Optional
from loguru import logger


# Base URL for the chatbot API
CHATBOT_BASE_URL = "https://heidelai-backend2.koyeb.app"

async def send_to_chatbot(
    customer_phone_no: str,
    message_content: str,
    timeout: float = 30.0,
    message_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Send a message to the HeidelAI chatbot and get a response.

    Args:
        customer_phone_no: Customer's phone number (will be prefixed with 'whatsapp_')
        message_content: The message content from the customer
        timeout: Request timeout in seconds (default: 30s)
        message_id: Optional WhatsApp wamid of the incoming message (forwarded to the backend)

    Returns:
        A dict containing at least ``"response"`` on success, optionally
        ``"reply_to_message_id"``; or None on any error.
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
        logger.info(f"Sending message to chatbot for {identifier}")
        logger.debug(f"Payload: {payload}")
        
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
            response_data = response.json()
            
            # Extract the chatbot's response
            chatbot_response = response_data.get("response")
            
            if chatbot_response:
                logger.info(f"Received response from chatbot for {identifier}")
                # logger.debug(f"Response: {chatbot_response[:100]}...")
                return response_data
            else:
                logger.error(f"No 'response' field in chatbot response: {response_data}")
                return None
                
    except httpx.TimeoutException:
        logger.error(f"Timeout while calling chatbot for {identifier}")
        return None

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error from chatbot: {e.response.status_code} - {e.response.text}")
        return None

    except Exception as e:
        logger.opt(exception=True).error(f"Unexpected error calling chatbot: {str(e)}")
        return None


def send_to_chatbot_sync(
    customer_phone_no: str,
    message_content: str,
    timeout: float = 30.0,
    message_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Synchronous version of send_to_chatbot for use in non-async contexts.

    Args:
        customer_phone_no: Customer's phone number (will be prefixed with 'whatsapp_')
        message_content: The message content from the customer
        timeout: Request timeout in seconds (default: 30s)
        message_id: Optional WhatsApp wamid of the incoming message (forwarded to the backend)

    Returns:
        A dict containing at least ``"response"`` on success, optionally
        ``"reply_to_message_id"``; or None on any error.
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
        logger.info(f"Sending message to chatbot for {identifier}")
        logger.debug(f"Payload: {payload}")
        
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
        response_data = response.json()
        
        # Extract the chatbot's response
        chatbot_response = response_data.get("response")
        
        if chatbot_response:
            logger.info(f"Received response from chatbot for {identifier}")
            logger.debug(f"Response: {chatbot_response[:100]}...")
            return response_data
        else:
            logger.error(f"No 'response' field in chatbot response: {response_data}")
            return None
            
    except requests.Timeout:
        logger.error(f"Timeout while calling chatbot for {identifier}")
        return None

    except requests.HTTPError as e:
        logger.error(f"HTTP error from chatbot: {e.response.status_code} - {e.response.text}")
        return None

    except Exception as e:
        logger.opt(exception=True).error(f"Unexpected error calling chatbot: {str(e)}")
        return None

if __name__ == "__main__":
    # Example usage
    import asyncio
    
    async def test_chatbot():
        response = await send_to_chatbot("919511907785", "Hello, how are you?")
        print("Chatbot response:", response)
    
    asyncio.run(test_chatbot())