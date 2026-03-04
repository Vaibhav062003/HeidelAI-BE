import httpx
from typing import Dict, Any, Optional
import logging

from datetime import datetime, timezone

from config import settings
from database import get_async_mongo_db
from schemas.models import ActionButton, InteractiveListMessage, InteractiveReplyButtonMessage, InteractiveUrlButtonMessage, ListRow, ListSection, ReplyButton
from services.wa_service import send_interactive_whatsapp_message

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
Handles 2-stage onboarding, saves state to MongoDB, routes to fallback agent by default.
"""

def conversations_col():
    return db[f"conversations_{ORG_ID}"]

MENU_MESSAGE_SECTIONS = [
    ListSection(
        title="Free Events",
        rows=[
            ListRow(id="free_master_class", title="Free Master Class", description="Join our free Vedic Maths Master Class"),
        ]
    ),
    ListSection(
        title="Student Programs",
        rows=[
            ListRow(id="abacus_students_5_to_9", title="Abacus for Students", description="For ages 5 to 9"),
            ListRow(id="vedic_maths_regular_9_plus", title="Regular Vedic Maths", description="For ages 9+"),
            ListRow(id="vedic_maths_summer_camp", title="Vedic Maths Summer Camp", description="Special vedic maths summer program for ages 9+"),
        ]
    ),
    ListSection(
        title="Educator Programs",
        rows=[
            ListRow(id="vedic_maths_teacher", title="Vedic Maths for Teachers", description="Comprehensive training for educators"),
        ]
    )
]

def get_menu_message(location: str):
    return InteractiveListMessage(
        type="list",
        body_text=(
            "Great!\n"
            f"I've noted that you are from {location}.\n\n"
            "We offer structured learning programs for Students and Teachers.\n\n"
            "We also conduct a *FREE Vedic Maths Master Class* every 2nd and 4th Saturday (8:00 PM – 9:30 PM).\n\n"
            "Please choose an option below."
        ),
        button_text="View Options",
        sections=MENU_MESSAGE_SECTIONS
    )

def get_welcome_message(sender_name: str):
    # Safely get the first name, default to "there" if sender_name is empty/None
    first_name = sender_name.split(' ')[0] if sender_name else "there"
    
    return InteractiveListMessage(
        type="list",
        header_text="Welcome to Parijat Academy",
        body_text=(
            f"Hello {first_name}👋🏻\n"
            "My name is Vedica and I am your virtual assistant.\n\n"
            "We offer structured learning programs for Students and Teachers.\n\n"
            "We also conduct a *FREE Vedic Maths Master Class* every 2nd and 4th Saturday (8:00 PM – 9:30 PM).\n\n"
            "Please choose an option below."
        ),
        button_text="View Options",
        sections=MENU_MESSAGE_SECTIONS
    )

def get_location_message(sender_name: str):
    # Safely get the first name, default to "there" if sender_name is empty/None
    first_name = sender_name.split(' ')[0] if sender_name else "there"

    return InteractiveReplyButtonMessage(
        type="button",
        header={
            "type": "text",
            "text": "Welcome to Parijat Academy"
        },
        body_text=(
            f"Hello {first_name}👋🏻\n"
            "My name is Vedica and I am your virtual assistant.\n\n"
            "To proceed, please let me know where you are based:\n\n"
        ),
        buttons=[
            ActionButton(
                type="reply",
                reply=ReplyButton(
                    id="india", 
                    title="India"
                )
            ),
            ActionButton(
                type="reply",
                reply=ReplyButton(
                    id="abroad", 
                    title="Abroad"
                )
            ),
        ],
    )

async def process_onboarding_if_needed(user_input: str, conversation_id: str, org_id: str, stage: str, whatsapp_business_id: str, sender_name: str):
    logger.info("Processing onboarding for conversation_id=%s at stage=%s with user_input=%s", conversation_id, stage, user_input)
    user_input = user_input.strip().lower()

    # ───────────────────────────────────
    # STAGE 1: Welcome
    # ───────────────────────────────────
    if stage == "welcome" or stage is None:
        # First message to be sent would always be current campaign text
        await send_interactive_whatsapp_message(
            org_id=org_id,
            conversation_id=conversation_id,
            sender_id=whatsapp_business_id,
            interactive_message=InteractiveUrlButtonMessage(
                type="cta_url",
                header={
                    "type": "image",
                    "image": {
                        "link": "https://res.cloudinary.com/djpc5zfgz/image/upload/v1772559110/intro_image_pma_p0tae1.jpg"
                    }
                },
                body_text=(
                    "🌟 FREE VEDIC MATHS MASTERCLASS 🌟\n\n"
                    "Does your child feel Exam Stress in Maths? 😟\n"
                    "✨ Now let the Exam Stress Just Go Away! ✨\n"
                    "Give your child the power of Vedic Maths and watch confidence grow! 🚀\n\n"
                    "🎯 In This FREE Masterclass Students Will Learn:\n"
                    "✅ Faster Calculations\n"
                    "✅ Better Accuracy\n"
                    "✅ Improved Concentration\n"
                    "✅ More Confidence in Exams\n"
                    "✅ Fear of Maths will disappear\n\n"
                    "🎓 100% FREE Session\n"
                    "🚀 Limited Seats, Register Now!\n\n" 
                    "🌐 www.paarijatacademy.in\n"
                    "📞 Call / WhatsApp:\n"
                    "8055579965 / 9503498556"
                ),
                button_text="Register Now",
                url="https://forms.gle/xWnKQCt8CVepAbYb9"
            )
        )

        reply = get_location_message(sender_name)

        await conversations_col().update_one(
            {"conversation_id": conversation_id},
            {"$set": {
                "onboarding_stage": "awaiting_location",
                "is_ai_enabled": True
            }}
        )
        
        return reply
    
    # ───────────────────────────────────
    # Edge Case: Menu always shown on hi hello hey
    # ───────────────────────────────────
    
    if user_input in ["hi", "hello", "hey"]:
        reply = get_welcome_message(sender_name)

        return reply

    # ───────────────────────────────────
    # STAGE 2: Awaiting Location
    # ───────────────────────────────────
    if stage == "awaiting_location":
        reply = get_location_message(sender_name)

        if user_input in ["india", "abroad"]:
            location = user_input
            persona = "fallback"

            agent = "agent_fallback"
            reply = get_menu_message(location)

            await conversations_col().update_one(
                {"conversation_id": conversation_id},
                {"$set": {
                    "onboarding_stage": "completed",
                    "location": location,
                    "persona": persona,
                    "assigned_bot": agent,
                    "is_ai_enabled": True,
                }}
            )
            
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