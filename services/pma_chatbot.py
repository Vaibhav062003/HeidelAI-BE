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

def get_introduction_message(sender_name: str):
    # Safely get the first name, default to "there" if sender_name is empty/None
    first_name = get_first_name(sender_name)

    return InteractiveReplyButtonMessage(
        type="button",
        header={
            "type": "text",
            "text": "Welcome to Parijat Academy"
        },
        body_text=(
            f"Hello {first_name}👋🏻\n\n"
            "Give your child a power-packed learning advantage this summer! 🚀\n\nAt Parijat Academy, we combine Vedic Maths + AI Basics to build:\n✅ Super-fast calculation skills\n✅ Sharp logical thinking\n✅ Confidence in academics\n✅ Future-ready AI awareness\n\n📍 Offline Summer Camp @ BVB Paranjape Vidya Mandir, Kothrud Pune\n📅 1st May to 15th May\n⏰ Morning 10 AM to 12.30pm\n\n⚠️ Limited Seats | Combo Offer Discount worth Rs 999/- \n📚 Free Study Material worth Rs. 499/-\n\n👉 Fill this form to reserve your child’s seat today!\n🔗   https://forms.gle/6ZoiPxw1op4PsByb9\n\n📲📞 Call/WhatsApp Now: 9503498556/7030617881\n🌐 www.paarijatacademy.in/ 🌐 www.aijunior.in"
        ),
        buttons=[
            ActionButton(
                type="reply",
                reply=ReplyButton(
                    id="vedic_maths_intro", 
                    title="Vedic Maths"
                )
            ),
            ActionButton(
                type="reply",
                reply=ReplyButton(
                    id="ai_intro", 
                    title="AI"
                )
            ),
            ActionButton(
                type="reply",
                reply=ReplyButton(
                    id="vedic_ai_intro", 
                    title="Vedic + AI"
                )
            ),
        ],
    )

def get_step_two_message():
    return InteractiveReplyButtonMessage(
        type="button",
        header={
            "type": "document",
            "document": {"link":"https://res.cloudinary.com/djpc5zfgz/image/upload/v1776864318/AI_Junior_Summer_Camp_10042026_260410_155346_m1acpr.pdf"}
        },
        body_text=(
            "Great Choice! 😊\n\n✨ In this Offline Summer Camp your child will learn:\n✅Vedic Maths - super fast calculation tricks\n✅Improve speed, accuracy & confidence\n✅AI Junior - Future ready learning (Document attached)\n\n📅2nd May - 16th May\n⏰10:00AM - 12:30PM\n📍@BVB Paranjpe Vidya Mandir, Kothrud, Pune\n\nWould you like to know fees and offers?\n\nPlease tap the button below👇🏻"
        ),
        buttons=[
            ActionButton(
                type="reply",
                reply=ReplyButton(
                    id="step_two_yes", 
                    title="Yes"
                )
            ),
        ],
    )

def get_step_three_ai_message():
    return InteractiveUrlButtonMessage(
        type="cta_url",
        body_text=(
            "Here are the details 😊\n\n💰 Course Fee: ₹3000 AI\n🎁Today's Special Offer:\n✅Free study material worth ₹499\n\nVisit our website - https://paarijatacademy.in/"
        ),
        button_text="Register now",
        url="https://forms.gle/6ZoiPxw1op4PsByb9"
    )

def get_step_three_vedic_ai_message():
    return InteractiveUrlButtonMessage(
        type="cta_url",
        body_text=(
            "Here are the details 😊\n\n💰 Course Fee: ₹3000 VM + ₹3000 AI\n🎁Today's Special Offer:\n✅Combo Discount ₹999\n✅Just to pay Rs.5000/- with combo offer\n✅Free study material worth ₹499\n\nVisit our website - https://paarijatacademy.in/"
        ),
        button_text="Register now",
        url="https://forms.gle/6ZoiPxw1op4PsByb9"
    )

async def process_onboarding_if_needed(user_input: str, conversation_id: str, org_id: str, stage: str, whatsapp_business_id: str, sender_name: str):
    logger.info(f"Processing onboarding for conversation_id={conversation_id} at stage={stage} with user_input={user_input}")
    user_input = user_input.strip().lower()

    # ───────────────────────────────────
    # STAGE 1: Welcome
    # ───────────────────────────────────
    if stage == "welcome" or stage is None:

        reply = get_introduction_message(sender_name)

        await conversations_col().update_one(
            {"conversation_id": conversation_id},
            {"$set": {
                "onboarding_stage": "awaiting_introduction",
                "is_ai_enabled": True
            }}
        )
        
        return reply
    
    # ───────────────────────────────────
    # Edge Case: Menu always shown on hi hello hey
    # ───────────────────────────────────
    
    if user_input in ["hi", "hello", "hey"]:
        reply = get_introduction_message(sender_name)

        return reply

    # ───────────────────────────────────
    # STAGE 2: Awaiting Introduction Response
    # ───────────────────────────────────
    if stage == "awaiting_introduction":
        match user_input:
            case "vedic_maths_intro":
                reply = get_location_message(sender_name)

                await conversations_col().update_one(
                    {"conversation_id": conversation_id},
                    {"$set": {
                        "onboarding_stage": "awaiting_location",
                        "is_ai_enabled": True
                    }}
                )
                
                return reply

            case "ai_intro" | "vedic_ai_intro":
                reply = get_step_two_message()

                location = "india"
                persona = "fallback"

                agent = "agent_fallback"

                await conversations_col().update_one(
                    {"conversation_id": conversation_id},
                    {"$set": {
                        "step_two_content": user_input,
                        "onboarding_stage": "completed",
                        "location": location,
                        "persona": persona,
                        "assigned_bot": agent,
                        "is_ai_enabled": True,
                    }}
                )
                
                return reply

            case _:
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
