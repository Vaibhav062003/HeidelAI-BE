import httpx
from typing import Dict
from loguru import logger

# Base URL for the chatbot API
CHATBOT_BASE_URL = "https://heidelai-chatbot-2.koyeb.app"
# CHATBOT_BASE_URL = "http://localhost:8000"  # For local testing with self-signed certs, ensure to disable SSL verification in httpx client

async def send_to_agent(
    conversation_id: str,
    user_input: str,
    persona: str,
    location: str,
    customer_name: str,
    org_id: str,
    source: str = "webhook",
    timeout: int = 10
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
    

    # # Authorization headers
    # headers={
    #     "Content-Type": "application/json",
    #     "X-Internal-Secret": X_INTERNAL_SECRET 
    # }
    
    # API endpoint
    endpoint = f"{CHATBOT_BASE_URL}/api/v2/chat"
    
    try:
        logger.info(f"Sending message to chatbot for {conversation_id}")
        logger.debug(f"Payload: {payload}")
        
        # Make async HTTP request
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint,
                json=payload,
            )
            
            # Check if request was successful
            response.raise_for_status()
            
            # Parse response
            response_data = response.json()
            
            # Extract the chatbot's response
            final_response = response_data.get("response")
            
            if final_response:
                logger.info(f"Received response from chatbot for {conversation_id}")
                return { "final_response" : final_response }
            else:
                logger.warning(f"Chatbot response missing 'response' field for {conversation_id}: {response_data}")
                return { "final_response" : "I apologize, but I'm having trouble generating a response right now. Please try again later."}

    except httpx.TimeoutException:
        logger.error(f"Timeout while calling chatbot for {conversation_id}")
        return { "final_response" : "I apologize, but I'm taking longer than usual to respond. Please try again in a moment."} 
        
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error from chatbot: {e.response.status_code} - {e.response.text}")
        return { "final_response" : "I apologize, but I'm experiencing technical difficulties. Please try again later."}  
        
    except Exception as e:
        logger.error(f"Unexpected error calling chatbot: {str(e)}", exc_info=True)
        return { "final_response" : "I apologize, but something went wrong. Please try again later."}  
    