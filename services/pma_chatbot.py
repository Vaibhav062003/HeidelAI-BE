import httpx
from typing import Dict, Any, Optional
import logging

from config.settings import X_INTERNAL_SECRET

# Configure logging
logger = logging.getLogger(__name__)

# Base URL for the chatbot API
CHATBOT_BASE_URL = "https://heidelai-chatbot-2.koyeb.app"

async def send_to_pma_chatbot(
    customer_phone_no: str,
    message_content: str,
    timeout: float = 30.0
) -> Optional[str]:
    """
    Send a message to the PMA (Parijat Math Academy) chatbot and get a response.
    
    Args:
        customer_phone_no: Customer's phone number (will be prefixed with 'whatsapp_')
        message_content: The message content from the customer
        timeout: Request timeout in seconds (default: 30s)
        
    Returns:
        The chatbot's response text, or None if there was an error
    """
    # Prepare the identifier with whatsapp_ prefix
    conversation_id = f"whatsapp_{customer_phone_no}"
    
    # Prepare the request payload
    payload = {
        "conversation_id": conversation_id,
        "message": message_content,
        "source": "webhook"
    }

    # Authorization headers
    headers={
        "Content-Type": "application/json",
        "X-Internal-Secret": X_INTERNAL_SECRET 
    }
    
    # API endpoint
    endpoint = f"{CHATBOT_BASE_URL}/api/v1/chat"
    
    try:
        logger.info(f"Sending message to chatbot for {conversation_id}")
        logger.debug(f"Payload: {payload}")
        
        # Make async HTTP request
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers=headers
            )
            
            # Check if request was successful
            response.raise_for_status()
            
            # Parse response
            response_data = response.json()
            
            # Extract the chatbot's response
            final_response = response_data.get("final_response")
            bot_context = response_data.get("bot_context")
            
            if final_response:
                logger.info(f"Received response from chatbot for {conversation_id}")
                # logger.debug(f"Response: {chatbot_response[:100]}...")
                return { "final_response" : final_response, "bot_context" : bot_context }
            else:
                logger.error(f"No 'final_response' field in chatbot response: {response_data}")
                return None
                
    except httpx.TimeoutException:
        logger.error(f"Timeout while calling chatbot for {conversation_id}")
        return { "final_response" : "I apologize, but I'm taking longer than usual to respond. Please try again in a moment.", "bot_context" : {} } 
        
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error from chatbot: {e.response.status_code} - {e.response.text}")
        return { "final_response" : "I apologize, but I'm experiencing technical difficulties. Please try again later.", "bot_context" : {} }  
        
    except Exception as e:
        logger.error(f"Unexpected error calling chatbot: {str(e)}", exc_info=True)
        return { "final_response" : "I apologize, but something went wrong. Please try again later.", "bot_context" : {} }  


def send_to_chatbot_sync(
    customer_phone_no: str,
    message_content: str,
    timeout: float = 30.0
) -> Optional[str]:
    """
    Synchronous version of send_to_chatbot for use in non-async contexts.
    
    Args:
        customer_phone_no: Customer's phone number (will be prefixed with 'whatsapp_')
        message_content: The message content from the customer
        timeout: Request timeout in seconds (default: 30s)
        
    Returns:
        The chatbot's response text, or None if there was an error
    """
    import requests
    
    # Prepare the identifier with whatsapp_ prefix
    identifier = f"whatsapp_{customer_phone_no}"
    
    # Prepare the request payload
    payload = {
        "identifier": identifier,
        "query": message_content
    }
    
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
            return chatbot_response
        else:
            logger.error(f"No 'response' field in chatbot response: {response_data}")
            return None
            
    except requests.Timeout:
        logger.error(f"Timeout while calling chatbot for {identifier}")
        return "I apologize, but I'm taking longer than usual to respond. Please try again in a moment."
        
    except requests.HTTPError as e:
        logger.error(f"HTTP error from chatbot: {e.response.status_code} - {e.response.text}")
        return "I apologize, but I'm experiencing technical difficulties. Please try again later."
        
    except Exception as e:
        logger.error(f"Unexpected error calling chatbot: {str(e)}", exc_info=True)
        return "I apologize, but something went wrong. Please try again later."
    
if __name__ == "__main__":
    # Example usage
    import asyncio
    
    async def test_chatbot():
        response = await send_to_pma_chatbot("917972510629", "Based in India from Pune my name is Rounak")
        print("Chatbot response:", response)
    
    asyncio.run(test_chatbot())