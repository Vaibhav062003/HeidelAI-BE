import httpx
from typing import Dict
from loguru import logger

from config import settings
from database import get_async_mongo_db
from schemas.models import (
    ActionButton,
    InteractiveListMessage,
    InteractiveReplyButtonMessage,
    ListRow,
    ListSection,
    ReplyButton,
)
from services.utils import get_first_name
from services.wa_service import send_whatsapp_message
from services.ai_reply_guard import AIProviderUnavailable, validate_ai_reply
from services.agent_service import _persist_handoff, _validated_handoff
from services.internal_request import signed_json

db = get_async_mongo_db()

ORG_ID = settings.PMA_ORG_ID

# TODO: Replace with the approved promotional post/video URL for the books flow.
VEDIC_MATHS_BOOKS_PROMO_VIDEO_URL = ""

# Base URL for the chatbot API
CHATBOT_BASE_URL = "https://backend-chatbot.koyeb.app/"
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
    reply_context_message_id: str = None,
    message_id: str = None
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

    if message_id:
        payload["message_id"] = message_id

    if reply_context_message_id:
        payload["reply_context_message_id"] = reply_context_message_id

    endpoint = f"{CHATBOT_BASE_URL}/api/v1/chat"

    try:
        body, headers = signed_json(payload, "/api/v1/chat", settings.INTERNAL_HMAC_SECRET, message_id)
        logger.info(f"Sending message to chatbot for ...{conversation_id[-6:]}")
        logger.debug(
            f"PMA chatbot payload org={org_id}, conversation=...{conversation_id[-4:]}, "
            f"input_length={len(user_input or '')}"
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint,
                content=body,
                headers=headers,
            )

            response.raise_for_status()
            try:
                response_data = response.json()
            except ValueError as exc:
                raise AIProviderUnavailable("malformed_response") from exc
            if not isinstance(response_data, dict):
                raise AIProviderUnavailable("malformed_response")
            handoff = _validated_handoff(response_data.get("handoff"))
            if handoff:
                await _persist_handoff(
                    handoff,
                    org_id=org_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    customer_name=customer_name,
                    customer_text=user_input,
                    source=source,
                )
            try:
                final_response = validate_ai_reply(response_data.get("response"))
            except AIProviderUnavailable as exc:
                if handoff:
                    raise AIProviderUnavailable(
                        exc.category,
                        upstream_status=exc.upstream_status,
                        escalation_required=False,
                    ) from exc
                raise
            logger.info(f"Received response from chatbot for ...{conversation_id[-6:]}")
            return {"final_response": final_response}

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

def get_introduction_message(sender_name: str):
    # Safely get the first name, default to "there" if sender_name is empty/None
    first_name = get_first_name(sender_name)

    return (
        f"Hello {first_name}👋🏻\n\n"
        "Give your child a power-packed learning advantage this summer! 🚀\n\nAt Parijat Academy, we combine Vedic Maths + AI Basics to build:\n✅ Super-fast calculation skills\n✅ Sharp logical thinking\n✅ Confidence in academics\n✅ Future-ready AI awareness\n\n👉🏻 Fill this form to reserve your child’s seat today!\n🔗   https://forms.gle/6ZoiPxw1op4PsByb9\n\n📲📞 Call/WhatsApp Now: 9503498556/7030617881\n🌐 www.paarijatacademy.in/ 🌐 www.aijunior.in"
    )

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


def get_vedic_maths_books_message():
    return InteractiveReplyButtonMessage(
        type="button",
        header={
            "type": "image",
            "image": {
                "link": "https://res.cloudinary.com/djpc5zfgz/image/upload/v1779986749/WhatsApp_Image_2026-05-20_at_3.13.09_PM_urq9ue.jpg",
            },
        },
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
        buttons=[
            ActionButton(
                type="reply",
                reply=ReplyButton(id="order_books", title="Order Books"),
            ),
        ],
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

        try:
            await send_whatsapp_message(
                org_id=org_id,
                mode="reply",
                message={
                    "sender_id" : whatsapp_business_id,
                    "conversation_id" : conversation_id,
                    "content": get_introduction_message(sender_name),
                }
            )
        except Exception:
            logger.exception(
                f"Failed to send onboarding introduction for conversation_id={conversation_id}; "
                "continuing to update onboarding stage to awaiting_location"
            )

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
