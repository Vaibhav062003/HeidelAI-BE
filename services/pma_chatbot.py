import httpx
from typing import Dict
from loguru import logger

from config import settings
from database import get_async_mongo_db
from schemas.models import (
    ActionButton,
    InteractiveListMessage,
    InteractiveReplyButtonMessage,
    InteractiveUrlButtonMessage,
    ListRow,
    ListSection,
    ReplyButton,
)
from services.utils import get_first_name

db = get_async_mongo_db()

ORG_ID = settings.PMA_ORG_ID

# TODO: Replace with the approved promotional post/video URL for the books flow.
VEDIC_MATHS_BOOKS_PROMO_VIDEO_URL = ""

# TODO: Replace with the exact Amazon product URL for the Vedic Maths Books.
VEDIC_MATHS_BOOKS_AMAZON_URL = "https://www.amazon.in/"

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
    timeout: int = 10,
) -> Dict:
    """
    Send a message to the PMA chatbot service and return the chatbot's response.
    """
    payload = {
        "conversation_id": conversation_id,
        "user_input": user_input,
        "persona": persona,
        "location": location,
        "customer_name": customer_name,
        "ORG_ID": org_id,
        "source": source,
    }

    endpoint = f"{CHATBOT_BASE_URL}/api/v1/chat"

    try:
        logger.info(f"Sending message to chatbot for {conversation_id}")
        logger.debug(f"Payload: {payload}")

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, json=payload)

            response.raise_for_status()
            response_data = response.json()
            final_response = response_data.get("response")

            if final_response:
                logger.info(f"Received response from chatbot for {conversation_id}")
                return {"final_response": final_response}

            logger.warning(f"Chatbot response missing 'response' field for {conversation_id}: {response_data}")
            return {"final_response": "I apologize, but I'm having trouble generating a response right now. Please try again later."}

    except httpx.TimeoutException:
        logger.error(f"Timeout while calling chatbot for {conversation_id}")
        return {"final_response": "I apologize, but I'm taking longer than usual to respond. Please try again in a moment."}

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error from chatbot: {e.response.status_code} - {e.response.text}")
        return {"final_response": "I apologize, but I'm experiencing technical difficulties. Please try again later."}

    except Exception as e:
        logger.opt(exception=True).error(f"Unexpected error calling chatbot: {str(e)}")
        return {"final_response": "I apologize, but something went wrong. Please try again later."}


"""
Parijat Academy - Onboarding Flow
Handles onboarding, saves state to MongoDB, and routes to fallback agent after menu selection.
"""


def conversations_col():
    return db[f"conversations_{ORG_ID}"]


MENU_MESSAGE_SECTIONS = [
    ListSection(
        title="Student Programs",
        rows=[
            ListRow(id="abacus_students_5_to_9", title="Abacus for Students", description="For ages 5 to 9"),
            ListRow(id="vedic_maths_regular_9_plus", title="Vedic Maths Program", description="For ages 9+"),
            ListRow(id="vedic_maths_books", title="Vedic Maths Books", description="Books for fast calculations and practice"),
        ],
    ),
    ListSection(
        title="Free Events",
        rows=[
            ListRow(id="free_master_class", title="Free Master Class", description="Join our free Vedic Maths Master Class"),
        ],
    ),
    ListSection(
        title="Educator Programs",
        rows=[
            ListRow(id="vedic_maths_teacher", title="Teacher Certification", description="Comprehensive training for educators"),
        ],
    ),
]


def get_menu_message(location: str):
    return InteractiveListMessage(
        type="list",
        body_text=(
            "Great!\n"
            f"I've noted that you are from {location}.\n\n"
            "We offer structured learning programs for Students and Teachers.\n\n"
            "We also conduct a *FREE Vedic Maths Master Class* every 2nd and 4th Saturday (8:00 PM - 9:30 PM).\n\n"
            "Please choose an option below."
        ),
        button_text="View Options",
        sections=MENU_MESSAGE_SECTIONS,
    )


def get_welcome_message(sender_name: str):
    first_name = get_first_name(sender_name)

    return InteractiveListMessage(
        type="list",
        header_text="Welcome to Parijat Academy",
        body_text=(
            f"Hello {first_name}\n"
            "My name is Vedica and I am your virtual assistant.\n\n"
            "We offer structured learning programs for Students and Teachers.\n\n"
            "We also conduct a *FREE Vedic Maths Master Class* every 2nd and 4th Saturday (8:00 PM - 9:30 PM).\n\n"
            "Please choose an option below."
        ),
        button_text="View Options",
        sections=MENU_MESSAGE_SECTIONS,
    )


def get_location_message(sender_name: str):
    first_name = get_first_name(sender_name)

    return InteractiveReplyButtonMessage(
        type="button",
        header={
            "type": "text",
            "text": "Welcome to Parijat Academy",
        },
        body_text=(
            f"Hello {first_name}\n"
            "My name is Vedica and I am your virtual assistant.\n\n"
            "To proceed, please let me know where you are based:\n\n"
        ),
        buttons=[
            ActionButton(
                type="reply",
                reply=ReplyButton(id="india", title="India"),
            ),
            ActionButton(
                type="reply",
                reply=ReplyButton(id="abroad", title="Abroad"),
            ),
        ],
    )


def get_introduction_message(sender_name: str):
    return get_location_message(sender_name)


def get_vedic_maths_books_message():
    header = None
    if VEDIC_MATHS_BOOKS_PROMO_VIDEO_URL:
        header = {
            "type": "video",
            "video": {"link": VEDIC_MATHS_BOOKS_PROMO_VIDEO_URL},
        }

    return InteractiveUrlButtonMessage(
        type="cta_url",
        header=header,
        body_text=(
            "🚀 Unlock the Power of Speed Maths with Vedic Mathematics! 📚✨\n\n"
            "Presenting the complete set of\n"
            "🔹 Volume 1\n"
            "🔹 Volume 2\n"
            "🔹 Volume 3\n"
            "🔹 Volume 4\n\n"
            "Specially designed for school students, competitive exams, Olympiads, Banking, JEE, CAT, UPSC, MPSC and more.\n\n"
            "🎉 Get the Complete Set at 20% DISCOUNT\n"
            "📦 Courier Charges Extra\n\n"
            "📞 Contact for Orders:\n"
            "📱 9503498556\n"
            "📱 8055579965\n\n"
            "🏢 Parijat Academy’s E-learning Pvt Ltd\n"
            "📍 Karve Nagar, Pune\n\n"
            "👨‍🏫 Author: Vijaay V Kulkarni\n"
            "Founder & Vedic Maths Coach\n"
            "🏆 Awarded \"Best Creative Excellence in Maths Coaching\" by CCE Finland\n\n"
            "🌐 Visit: www.paarijatacademy.in\n\n"
            "✨ Learn Faster\n"
            "✨ Calculate Smarter\n"
            "✨ Become a Mathematician Now!"
        ),
        button_text="Buy on Amazon",
        url=VEDIC_MATHS_BOOKS_AMAZON_URL,
    )


async def process_onboarding_if_needed(
    user_input: str,
    conversation_id: str,
    org_id: str,
    stage: str,
    whatsapp_business_id: str,
    sender_name: str,
):
    logger.info(f"Processing onboarding for conversation_id={conversation_id} at stage={stage} with user_input={user_input}")
    user_input = user_input.strip().lower()

    # STAGE 1: Welcome
    if stage == "welcome" or stage is None:
        reply = get_location_message(sender_name)

        await conversations_col().update_one(
            {"conversation_id": conversation_id},
            {"$set": {
                "onboarding_stage": "awaiting_location",
                "is_ai_enabled": True,
            }},
        )

        return reply

    # Edge Case: Menu always shown on hi hello hey
    if user_input in ["hi", "hello", "hey"]:
        return get_location_message(sender_name)

    # Legacy stage from the removed introduction flow.
    if stage == "awaiting_introduction":
        reply = get_location_message(sender_name)

        await conversations_col().update_one(
            {"conversation_id": conversation_id},
            {"$set": {
                "onboarding_stage": "awaiting_location",
                "is_ai_enabled": True,
            }},
        )

        return reply

    # STAGE 2: Awaiting Location
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
                }},
            )

        return reply

    if stage == "completed":
        return None


if __name__ == "__main__":
    import asyncio

    async def test_chatbot():
        response = await send_to_pma_chatbot("whatsapp_919511907785", "Hi", "parent_student", "india", "John Doe", ORG_ID, "webhook")
        print("Chatbot response:", response)

    asyncio.run(test_chatbot())
