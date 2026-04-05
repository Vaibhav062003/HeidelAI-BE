import httpx
from typing import Dict, Any, Optional
from loguru import logger

from datetime import datetime, timezone

from config import settings
from database import get_async_mongo_db
from schemas.models import ActionButton, InteractiveListMessage, InteractiveReplyButtonMessage, InteractiveUrlButtonMessage, ListRow, ListSection, ReplyButton
from services.utils import get_first_name
from services.wa_service import send_interactive_whatsapp_message

db = get_async_mongo_db()

ORG_ID = settings.PMA_ORG_ID

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
        logger.opt(exception=True).error(f"Unexpected error calling chatbot: {str(e)}")
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
    first_name = get_first_name(sender_name)
    
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
    first_name = get_first_name(sender_name)

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
    logger.info(f"Processing onboarding for conversation_id={conversation_id} at stage={stage} with user_input={user_input}")
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
                        "link": "https://res.cloudinary.com/djpc5zfgz/image/upload/v1773070610/WhatsApp_Image_2026-03-09_at_3.02.13_PM_tcdpby.jpg"
                    }
                },
                body_text="☀️ This Summer, Make Maths Your Child’s Superpower!\n🚀 Parijat Academy presents\n🎯 VEDIC MATHS SUMMER CAMP 2026\n\nGive your child the gift of faster calculations, sharper brain power & more confidence in Maths!\n📚 In this 15-Day Power Camp students will learn:\n✔ Super Fast Calculations\n✔ Tricks for Multiplication & Division\n✔ Improve Speed & Accuracy\n✔ Boost Confidence in Maths\n\n👦👧 Perfect for students aged 9+\n\n📅 Camp Dates:\n1st April – 15th April 2026\n🕰️ Eve 4 to 5.30 PM\n\n⚡ Limited Seats Available – Book Now!\n\n📞 Call / WhatsApp to Register:\n📱 9503498556\n📱 8055579965\n\n✨ Let’s Make Maths Your Child’s Best Friend!",                
                button_text="Register Now",
                url="https://forms.gle/hPMw7inpe7H1E4gF9"
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
