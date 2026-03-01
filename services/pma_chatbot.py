import httpx
from typing import Dict, Any, Optional
import logging

from datetime import datetime, timezone

from config import settings
from database import get_async_mongo_db
from schemas.models import InteractiveWhatsAppMessage, InteractiveWhatsAppMessageRow, InteractiveWhatsAppMessageSection

db = get_async_mongo_db()

ORG_ID = settings.PMA_ORG_ID

# Configure logging
logger = logging.getLogger(__name__)

# Base URL for the chatbot API
CHATBOT_BASE_URL = "https://heidelai-chatbot-2.koyeb.app"
# CHATBOT_BASE_URL = "http://localhost:8000"  # For local testing with self-signed certs, ensure to disable SSL verification in httpx client

async def send_to_pma_chatbot(
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
    Send a message to the PMA chatbot service and return the chatbot's response.
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
        A dictionary containing the key ``"final_response"`` on success, or ``None`` if the chatbot response is missing
        the expected data.
    """
    # Prepare the request payload
    payload = {
            "conversation_id": conversation_id,
            "user_input": user_input,
            "persona": persona,
            "location": location,
            "customer_name": customer_name,
            "ORG_ID": org_id,
            "source": source
        }
    

    # # Authorization headers
    # headers={
    #     "Content-Type": "application/json",
    #     "X-Internal-Secret": X_INTERNAL_SECRET 
    # }
    
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

"""
Parijat Academy - Onboarding Flow
Handles 2-stage onboarding, saves state to MongoDB, routes to persona agent.
"""

def conversations_col():
    return db[f"conversations_{ORG_ID}"]


def messages_col():
    return db[f"messages_{ORG_ID}"]


PERSONA_MAP = {
    "1": "teacher_tutor",
    "2": "parent_student",
    "3": "school_institution",
    "4": "franchise_investor",
}

LOCATION_MAP = {
    "1": "india",
    "2": "abroad",
}

# Maps persona → which bot/agent handles next messages
AGENT_MAP = {
    "teacher_tutor":       "agent_teacher",
    "parent_student":      "agent_parent",
    "school_institution":  "agent_school",
    "franchise_investor":  "agent_franchise",
    "fallback":            "agent_fallback",
}

# WELCOME_MESSAGE = (
#     "Hi! Welcome to Parijat Academy.\n"
#     "What brings you here today? Please tell me who you are:\n"
#     "1. Teacher or Tutor\n"
#     "2. Parent or Student\n"
#     "3. School or Institution\n"
#     "4. Franchise Investor\n"
#     "Enter the number corresponding to your choice."
# )

WELCOME_MESSAGE = InteractiveWhatsAppMessage(
    header_text="Welcome to Parijat Academy",
    body_text="Hello 👋🏻, \n\n What brings you here today? Please select an option from below to continue.",
    button_text="Options",
    sections=[
        InteractiveWhatsAppMessageSection(
            rows=[
                InteractiveWhatsAppMessageRow(id="1", title="Teacher or Tutor"),
                InteractiveWhatsAppMessageRow(id="2", title="Parent or Student"),
                InteractiveWhatsAppMessageRow(id="3", title="School or Institution"),
                InteractiveWhatsAppMessageRow(id="4", title="Franchise Investor"),
            ]
        )
    ]
)

# LOCATION_MESSAGE = (
#     "Got it! Are you based in India or abroad?\n"
#     "1. India\n"
#     "2. Abroad\n"
#     "Enter the number corresponding to your choice."
# )

LOCATION_MESSAGE = InteractiveWhatsAppMessage(
    body_text="Got it! Are you based in India or abroad?",
    button_text="Options",
    sections=[
        InteractiveWhatsAppMessageSection(
            rows=[
                InteractiveWhatsAppMessageRow(id="1", title="India"),
                InteractiveWhatsAppMessageRow(id="2", title="Abroad"),
            ]
        )
    ]
)

FALLBACK_MESSAGE = (
    "Sorry, I didn't quite catch that. Let me connect you with our team!"
)

async def get_conversation(conversation_id: str) -> dict | None:
    return await conversations_col().find_one({"conversation_id": conversation_id})


async def save_onboarding_attrs(conversation_id: str, org_id: str, update: dict):
    await conversations_col().update_one(
        {"conversation_id": conversation_id},
        {"$set": {
            **update,
            "last_message_timestamp": datetime.now(timezone.utc)
        }},
        upsert=True,
    )


# def save_message(conversation_id: str, role: str, text: str):
#     messages_col().insert_one({
#         "conversation_id": conversation_id,
#         "role": role,     
#         "content": text,
#         "timestamp": datetime.now(timezone.utc),
#     })

def _handoff_message(persona: str, location: str) -> str:
    labels = {
        "teacher_tutor":      "Teachers & Tutors",
        "parent_student":     "Parents & Students",
        "school_institution": "Schools & Institutions",
        "franchise_investor": "Franchise Investors",
    }
    label = labels.get(persona, "")
    loc_label = "India" if location == "india" else "international partners"
    return (
        f"Great! I've noted that you're from {loc_label} and are connecting as part of our {label} community. "
        f"Let me connect you with the right specialist. How can I help you today?"
    )


async def process_onboarding_if_needed(conversation_id: str, user_input: str, stage: str):
    user_input = user_input.strip().strip('.').lower()

    # ───────────────────────────────────
    # STAGE 1: Welcome
    # ───────────────────────────────────
    if stage == "welcome" or stage is None:
        reply = WELCOME_MESSAGE
        await conversations_col().update_one(
            {"conversation_id": conversation_id},
            {"$set": {
                "onboarding_stage": "awaiting_persona",
                "is_ai_enabled": True
            }}
        )
        
        return reply

    # ───────────────────────────────────
    # STAGE 2: Awaiting Persona
    # ───────────────────────────────────
    if stage == "awaiting_persona":
        if user_input in PERSONA_MAP:
            persona = PERSONA_MAP[user_input]
            reply = LOCATION_MESSAGE

            await conversations_col().update_one(
                {"conversation_id": conversation_id},
                {"$set": {
                    "onboarding_stage": "awaiting_location",
                    "persona": persona,
                }}
            )
        else:
            # Invalid - fallback persona
            await conversations_col().update_one(
                {"conversation_id": conversation_id},
                {"$set": {
                    "onboarding_stage": "completed",
                    "persona": "fallback",
                    "assigned_bot": AGENT_MAP["fallback"],
                    "is_ai_enabled": True,
                }}
            )
            reply = FALLBACK_MESSAGE

        # await messages_col(org_id).insert_one({
        #     "conversation_id": conversation_id,
        #     "role": "bot",
        #     "content": reply,
        #     "timestamp": datetime.now(timezone.utc)
        # })
        
        if reply == FALLBACK_MESSAGE:
            return None
        
        return reply

    # ───────────────────────────────────
    # STAGE 3: Awaiting Location
    # ───────────────────────────────────
    if stage == "awaiting_location":

        if user_input in LOCATION_MAP:
            location = LOCATION_MAP[user_input]

            convo = await get_conversation(conversation_id)
            if convo is None:
                persona = "fallback"
            else:
                persona = convo.get("persona", "fallback")

            agent = AGENT_MAP.get(persona, "agent_fallback")
            reply = _handoff_message(persona, location)

            await conversations_col().update_one(
                {"conversation_id": conversation_id},
                {"$set": {
                    "onboarding_stage": "completed",
                    "location": location,
                    "assigned_bot": agent,
                    "is_ai_enabled": True,
                }}
            )
        else:
            reply = FALLBACK_MESSAGE
            await conversations_col().update_one(
                {"conversation_id": conversation_id},
                {"$set": {
                    "onboarding_stage": "completed",
                    "persona": "fallback",
                    "location": "unknown",
                    "assigned_bot": AGENT_MAP["fallback"],
                    "is_ai_enabled": True,
                }}
            )

       # await messages_col(org_id).insert_one({
        #     "conversation_id": conversation_id,
        #     "role": "bot",
        #     "content": reply,
        #     "timestamp": datetime.now(timezone.utc)
        # })
        
        if reply == FALLBACK_MESSAGE:
            return None
        
        return reply


    if stage == "completed":
        return None
    
if __name__ == "__main__":
    # Example usage
    import asyncio
    
    async def test_chatbot():
        response = await send_to_pma_chatbot("whatsapp_919511907785", "Hi", "parent_student", "india", "John Doe", ORG_ID, "webhook")
        print("Chatbot response:", response)
    
    asyncio.run(test_chatbot())