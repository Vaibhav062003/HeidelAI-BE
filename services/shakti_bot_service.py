import asyncio
import re
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from loguru import logger

from database import get_async_mongo_db
from schemas.models import WhatsAppInteractiveMessage
from services.wa_service import send_interactive_whatsapp_message, send_whatsapp_message

async_db = get_async_mongo_db()

# --- CONSTANTS & DATA ---

SHAKTI_ORG_ID = "org_2zV2OOhodLocEnTybzR8t9MIYCE" # We will intercept this org_id in webhook_routes.py

BIKES = {
    "jawa": [
        {"id": "jawa_42", "title": "Jawa 42", "price": "₹1,84,942", "img": "https://jawayezdi.com/images/jawa42.jpg"},
        {"id": "jawa_42_fj", "title": "Jawa 42 FJ", "price": "₹1,54,551", "img": "https://jawayezdi.com/images/jawa42fj.jpg"},
        {"id": "jawa_42_bobber", "title": "Jawa 42 Bobber", "price": "₹2,08,942", "img": "https://jawayezdi.com/images/bobber.jpg"},
        {"id": "jawa_350", "title": "Jawa 350", "price": "₹2,15,815", "img": "https://jawayezdi.com/images/jawa350.jpg"},
        {"id": "jawa_perak", "title": "Jawa Perak", "price": "₹2,15,815", "img": "https://jawayezdi.com/images/perak.jpg"}
    ],
    "yezdi": [
        {"id": "yezdi_roadster", "title": "Yezdi Roadster", "price": "~₹1,98,000", "img": "https://jawayezdi.com/images/roadster.jpg"},
        {"id": "yezdi_scrambler", "title": "Yezdi Scrambler", "price": "~₹2,14,900", "img": "https://jawayezdi.com/images/scrambler.jpg"},
        {"id": "yezdi_adventure", "title": "Yezdi Adventure", "price": "~₹2,14,900", "img": "https://jawayezdi.com/images/adventure.jpg"}
    ],
    "bsa": [
        {"id": "bsa_gold_star", "title": "BSA Gold Star 650", "price": "₹2,99,995", "img": "https://bsa.com/images/goldstar.jpg"}
    ]
}

OUTLETS = [
    {"id": "out_ravet", "title": "Ravet Showroom", "desc": "Survey No 288, Pune Bhir Plaza", "url": "https://maps.google.com/?q=Ravet+Jawa+Yezdi"},
    {"id": "out_pimpri", "title": "Pimpri Chinchwad", "desc": "Kasarwadi, Pune", "url": "https://maps.google.com/?q=Pimpri+Jawa+Yezdi"},
    {"id": "out_mundhwa", "title": "Mundhwa / Kharadi", "desc": "S.No.1, Nyati, Pune", "url": "https://maps.google.com/?q=Mundhwa+Jawa"},
    {"id": "out_baner", "title": "Baner", "desc": "GP Citi Capital, Pune", "url": "https://maps.google.com/?q=Baner+Jawa"}
]

FAQS = [
    {"id": "faq_warranty", "title": "Warranty", "answer": "2 years standard, extendable."},
    {"id": "faq_service", "title": "Service Interval", "answer": "Every 6 months or 6000 km whichever comes first."},
    {"id": "faq_rsa", "title": "Roadside Assistance", "answer": "Included at time of purchase."},
    {"id": "faq_insurance", "title": "Insurance", "answer": "Available at time of purchase."},
    {"id": "faq_delivery", "title": "Delivery Time", "answer": "No home delivery (showroom pickup only): ~10 days from booking."},
    {"id": "faq_tradein", "title": "Trade-In", "answer": "Accepted. Bring your bike to any outlet."},
    {"id": "faq_testrides", "title": "Test Rides", "answer": "Available by appointment at all outlets."}
]

# --- SESSION MGMT ---

async def get_session(org_id: str, phone_number: str) -> dict:
    col = async_db[f"shakti_sessions_{org_id}"]
    session = await col.find_one({"phone_number": phone_number})
    if not session:
        session = {
            "phone_number": phone_number,
            "first_name": None,
            "state": "S1_WELCOME",
            "context": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc)
        }
        await col.insert_one(session)
    return session

async def update_session(org_id: str, phone_number: str, state: str, context_updates: dict = None, first_name: str = None):
    col = async_db[f"shakti_sessions_{org_id}"]
    updates = {"state": state, "updated_at": datetime.now(timezone.utc)}
    if first_name is not None:
        updates["first_name"] = first_name
        
    update_op = {"$set": updates}
    
    if context_updates:
        for k, v in context_updates.items():
            update_op["$set"][f"context.{k}"] = v
            
    await col.update_one({"phone_number": phone_number}, update_op)

# --- MESSAGING HELPERS ---

async def _send_text(org_id, whatsapp_id, to_number, text):
    try:
        await send_whatsapp_message(
            org_id=org_id,
            mode="reply",
            message={
                "sender_id": whatsapp_id,
                "conversation_id": to_number,
                "content": text,
            }
        )
    except Exception as e:
        logger.error(f"[SHAKTI] Failed to send text to {to_number}: {e}")

from pydantic import TypeAdapter

InteractiveMessageAdapter = TypeAdapter(WhatsAppInteractiveMessage)

async def _send_interactive(org_id, whatsapp_id, to_number, payload_dict):
    try:
        validated = InteractiveMessageAdapter.validate_python(payload_dict)
        await send_interactive_whatsapp_message(
            org_id=org_id,
            conversation_id=to_number,
            sender_id=whatsapp_id,
            interactive_message=validated
        )
    except Exception as e:
        logger.error(f"[SHAKTI] Failed to send interactive to {to_number}: {e}")

# --- BOT LOGIC ---

async def handle_shakti_message(
    org_id: str, 
    conversation_id: str, 
    message_type: str, 
    content: str, 
    sender_name: str,
    whatsapp_business_id: str,
    message_dict: dict = None
):
    # If this is an interactive message, extract the selection ID since the webhook hasn't yet
    if message_type == "interactive" and message_dict:
        interactive_data = message_dict.get("interactive", {})
        interaction_type = interactive_data.get("type")
        if interaction_type == "list_reply":
            content = interactive_data.get("list_reply", {}).get("id")
        elif interaction_type == "button_reply":
            content = interactive_data.get("button_reply", {}).get("id")
            
    # conversation_id is usually the customer phone number in this context
    customer_phone = conversation_id
    logger.info(f"[SHAKTI] Handling msg from {customer_phone}: type={message_type} content={content}")
    
    session = await get_session(org_id, customer_phone)
    current_state = session.get("state")
    context = session.get("context", {})
    
    # Global Keywords Override
    text_content = content.strip().lower() if message_type == "text" and isinstance(content, str) else ""
    if text_content in ["hi", "hello", "hey", "menu", "start"]:
        if session.get("first_name"):
            await update_session(org_id, customer_phone, "S2_MAIN_MENU")
            return await send_main_menu(org_id, whatsapp_business_id, customer_phone, session.get("first_name"))
        else:
            await update_session(org_id, customer_phone, "S1_WELCOME")
            return await send_welcome(org_id, whatsapp_business_id, customer_phone)
            
    if text_content in ["agent", "human", "talk to a person", "help"]:
        await update_session(org_id, customer_phone, "S8_AGENT")
        return await _send_text(org_id, whatsapp_business_id, customer_phone, "I'll connect you with our team — someone will reply here shortly. In the meantime, is there anything I can note down for them?")

    # State Machine Routing
    if current_state == "S1_WELCOME":
        if message_type == "text":
            name = text_content.title() if len(text_content) < 30 else sender_name
            await update_session(org_id, customer_phone, "S2_MAIN_MENU", first_name=name)
            return await send_main_menu(org_id, whatsapp_business_id, customer_phone, name)

    elif current_state == "S2_MAIN_MENU":
        if message_type == "interactive":
            if content == "menu_browse":
                await update_session(org_id, customer_phone, "S3A_BRAND")
                return await send_brand_selection(org_id, whatsapp_business_id, customer_phone)
            elif content == "menu_dealer":
                await update_session(org_id, customer_phone, "S4A_ASK_PINCODE", {"next_action": "dealer"})
                return await _send_text(org_id, whatsapp_business_id, customer_phone, "Great! Could you please share your 6-digit Pincode or City name so I can locate the nearest showroom?")
            elif content == "menu_test_ride":
                await update_session(org_id, customer_phone, "S4A_ASK_PINCODE", {"next_action": "test_ride"})
                return await _send_text(org_id, whatsapp_business_id, customer_phone, "Great! Could you please share your 6-digit Pincode or City name so we can find the nearest showroom for your test ride?")
            elif content == "menu_service":
                await update_session(org_id, customer_phone, "S4A_ASK_PINCODE", {"next_action": "service"})
                return await _send_text(org_id, whatsapp_business_id, customer_phone, "Sure! Could you please share your 6-digit Pincode or City name so we can find the nearest service center?")
            elif content == "menu_faqs":
                await update_session(org_id, customer_phone, "S7A_FAQ")
                return await send_faqs(org_id, whatsapp_business_id, customer_phone)
            elif content == "menu_agent":
                await update_session(org_id, customer_phone, "S8_AGENT")
                return await _send_text(org_id, whatsapp_business_id, customer_phone, "I'll connect you with our team — someone will reply here shortly.")

    elif current_state == "S3A_BRAND":
        if message_type == "interactive" and content.startswith("brand_"):
            brand = content.replace("brand_", "")
            await update_session(org_id, customer_phone, "S3B_MODELS", {"brand": brand})
            return await send_models(org_id, whatsapp_business_id, customer_phone, brand)
            
    elif current_state == "S3B_MODELS":
        if message_type == "interactive" and content.startswith("model_"):
            model_id = content.replace("model_", "")
            await update_session(org_id, customer_phone, "S3C_DETAIL", {"model": model_id})
            return await send_model_detail(org_id, whatsapp_business_id, customer_phone, model_id, context.get("brand", "jawa"))
            
    elif current_state == "S3C_DETAIL":
        if message_type == "interactive":
            if content == "act_test_ride":
                return await send_test_ride_cta(org_id, whatsapp_business_id, customer_phone)
            elif content == "act_dealer":
                await update_session(org_id, customer_phone, "S4A_ASK_PINCODE", {"next_action": "dealer"})
                return await _send_text(org_id, whatsapp_business_id, customer_phone, "Great! Could you please share your 6-digit Pincode or City name so I can locate the nearest showroom?")
            elif content == "act_menu":
                await update_session(org_id, customer_phone, "S2_MAIN_MENU")
                return await send_main_menu(org_id, whatsapp_business_id, customer_phone, session.get("first_name", sender_name))

    elif current_state == "S4A_ASK_PINCODE":
        if message_type == "text":
            # Save pincode/city to session
            await update_session(org_id, customer_phone, "S2_MAIN_MENU", {"location": text_content})
            
            # Route based on the original intent
            next_act = context.get("next_action")
            if next_act == "test_ride":
                return await send_test_ride_cta(org_id, whatsapp_business_id, customer_phone)
            elif next_act == "service":
                return await send_service_cta(org_id, whatsapp_business_id, customer_phone)
            else:
                # Default to showing outlets
                await update_session(org_id, customer_phone, "S4B_OUTLETS")
                return await send_outlets(org_id, whatsapp_business_id, customer_phone)

    elif current_state == "S4B_OUTLETS":
        if message_type == "interactive" and content.startswith("out_"):
            outlet = next((o for o in OUTLETS if o["id"] == content), OUTLETS[0])
            payload = {
                "type": "cta_url",
                "body_text": f"*{outlet['title']}*\n{outlet['desc']}",
                "cta_label": "Get Directions",
                "url": outlet["url"]
            }
            await _send_interactive(org_id, whatsapp_business_id, customer_phone, payload)
            await asyncio.sleep(1)
            back_payload = {
                "type": "button",
                "body_text": "Need anything else?",
                "buttons": [{"type": "reply", "reply": {"id": "act_menu", "title": "Back to Menu"}}]
            }
            return await _send_interactive(org_id, whatsapp_business_id, customer_phone, back_payload)

    elif current_state == "S7A_FAQ":
        if message_type == "interactive" and content.startswith("faq_"):
            faq_id = content
            await update_session(org_id, customer_phone, "S7B_FAQ_ANSWER")
            return await send_faq_answer(org_id, whatsapp_business_id, customer_phone, faq_id)

    elif current_state == "S7B_FAQ_ANSWER":
        if message_type == "interactive":
            if content == "faq_back":
                await update_session(org_id, customer_phone, "S7A_FAQ")
                return await send_faqs(org_id, whatsapp_business_id, customer_phone)
            elif content == "menu_main" or content == "act_menu":
                await update_session(org_id, customer_phone, "S2_MAIN_MENU")
                return await send_main_menu(org_id, whatsapp_business_id, customer_phone, session.get("first_name", sender_name))

    # Catch-all fallback
    await _send_text(org_id, whatsapp_business_id, customer_phone, "I didn't quite catch that. Type 'menu' to go back to the main options.")

# --- RESPONSE GENERATORS ---

async def send_welcome(org_id, wa_id, phone):
    text = "Hi! I'm Vela, Shakti Motors' assistant — here to help with Jawa, Yezdi, and BSA.\n\nWhat should I call you? (Or type 'skip'.)"
    await _send_text(org_id, wa_id, phone, text)

async def send_main_menu(org_id, wa_id, phone, name):
    greeting = f"Nice to meet you, {name}!" if name and name.lower() != "skip" else "Welcome!"
    payload = {
        "type": "list",
        "body_text": f"{greeting} What can I help with today?",
        "button_text": "Select Option",
        "sections": [{
            "title": "Main Menu",
            "rows": [
                {"id": "menu_browse", "title": "Browse Bikes"},
                {"id": "menu_dealer", "title": "Find a Dealer"},
                {"id": "menu_test_ride", "title": "Book a Test Ride"},
                {"id": "menu_service", "title": "Book a Service"},
                {"id": "menu_faqs", "title": "FAQs & Policies"},
                {"id": "menu_agent", "title": "Talk to a Person"}
            ]
        }]
    }
    await _send_interactive(org_id, wa_id, phone, payload)

async def send_brand_selection(org_id, wa_id, phone):
    payload = {
        "type": "button",
        "body_text": "Which brand are you exploring?",
        "buttons": [
            {"type": "reply", "reply": {"id": "brand_jawa", "title": "Jawa"}},
            {"type": "reply", "reply": {"id": "brand_yezdi", "title": "Yezdi"}},
            {"type": "reply", "reply": {"id": "brand_bsa", "title": "BSA"}}
        ]
    }
    await _send_interactive(org_id, wa_id, phone, payload)

async def send_models(org_id, wa_id, phone, brand):
    brand_bikes = BIKES.get(brand, BIKES["jawa"])
    rows = [{"id": f"model_{b['id']}", "title": b["title"], "description": f"From {b['price']} ex-showroom"} for b in brand_bikes]
    
    payload = {
        "type": "list",
        "body_text": f"Here are the {brand.title()} models. Which one would you like to look at?",
        "button_text": "View Models",
        "sections": [{"title": f"{brand.title()} Bikes", "rows": rows[:10]}]
    }
    await _send_interactive(org_id, wa_id, phone, payload)

async def send_model_detail(org_id, wa_id, phone, model_id, brand):
    brand_bikes = BIKES.get(brand, BIKES["jawa"])
    bike = next((b for b in brand_bikes if b["id"] == model_id), brand_bikes[0])
    
    payload = {
        "type": "button",
        "body_text": f"*{bike['title']}*\nStarts from {bike['price']} (ex-showroom)\n\nExact on-road price depends on your city.",
        "buttons": [
            {"type": "reply", "reply": {"id": "act_test_ride", "title": "Book Test Ride"}},
            {"type": "reply", "reply": {"id": "act_dealer", "title": "Find Dealer"}},
            {"type": "reply", "reply": {"id": "act_menu", "title": "Back to Menu"}}
        ]
    }
    if bike.get("img"):
        payload["header"] = {"type": "image", "image": {"link": bike["img"]}}
        
    await _send_interactive(org_id, wa_id, phone, payload)

async def send_test_ride_cta(org_id, wa_id, phone):
    payload = {
        "type": "cta_url",
        "body_text": "Let's get your test ride booked. Fill out this quick form and our team will confirm your slot.",
        "cta_label": "Book Test Ride",
        "url": "https://docs.google.com/forms/d/e/1FAIpQLSc10b6GwCw8NsrqX6DY1PtL9AIdE8XDV_SJa_f7Ji_8IZEkZQ/viewform?usp=publish-editor"
    }
    await _send_interactive(org_id, wa_id, phone, payload)

async def send_service_cta(org_id, wa_id, phone):
    payload = {
        "type": "cta_url",
        "body_text": "Need a service? Schedule an appointment at any of our outlets.",
        "cta_label": "Book Service",
        "url": "https://docs.google.com/forms/d/e/1FAIpQLSeF91ySQvpjxaQqjywzvIcuJ9aagDE_Q91oEiuOXdbIzbhjZg/viewform?usp=publish-editor"
    }
    await _send_interactive(org_id, wa_id, phone, payload)

async def send_outlets(org_id, wa_id, phone):
    rows = [{"id": out["id"], "title": out["title"], "description": out["desc"]} for out in OUTLETS]
    payload = {
        "type": "list",
        "body_text": "Here are our Shakti Motors outlets in Pune. Select one for directions.",
        "button_text": "View Outlets",
        "sections": [{"title": "Showrooms", "rows": rows}]
    }
    await _send_interactive(org_id, wa_id, phone, payload)

async def send_faqs(org_id, wa_id, phone):
    rows = [{"id": f["id"], "title": f["title"]} for f in FAQS]
    payload = {
        "type": "list",
        "body_text": "What would you like to know?",
        "button_text": "View FAQs",
        "sections": [{"title": "Policies & Info", "rows": rows}]
    }
    await _send_interactive(org_id, wa_id, phone, payload)

async def send_faq_answer(org_id, wa_id, phone, faq_id):
    faq = next((f for f in FAQS if f["id"] == faq_id), FAQS[0])
    payload = {
        "type": "button",
        "body_text": f"*{faq['title']}*\n{faq['answer']}",
        "buttons": [
            {"type": "reply", "reply": {"id": "faq_back", "title": "Ask something else"}},
            {"type": "reply", "reply": {"id": "menu_main", "title": "Back to Menu"}}
        ]
    }
    await _send_interactive(org_id, wa_id, phone, payload)
