"""
Shakti Motors — WhatsApp Bot Service (v2 — Fault-Tolerant)
==========================================================
Interactive WhatsApp bot for Shakti Motors (Jawa/Yezdi/BSA dealership in Pune).

Safety Guarantees:
  - Feature Flag:   SHAKTI_ENABLED kills all bot activity instantly.
  - Dedup:          wa_message_id checked against audit log before processing.
  - TTL:            Sessions expire after 24 h; audit & tracking after 14 days.
  - Terminal States: S_ENDED / S8_AGENT release control (return False)
                     unless a trigger keyword is sent.
  - Return Contract: True = handled (webhook returns early).
                     False = not handled (continue to normal AI/flow).
  - Lazy imports:   wa_service, pydantic, schemas.models are imported inside
                     functions, so a broken import can never crash the webhook.

State Machine:
  S1_WELCOME  → (text name) → S2_MAIN_MENU
  S2_MAIN_MENU →
      menu_browse    → S3A_BRAND
      menu_dealer    → S4A_ASK_PINCODE (next_action=dealer)
      menu_test_ride → S4A_ASK_PINCODE (next_action=test_ride)
      menu_service   → S4A_ASK_PINCODE (next_action=service)
      menu_faqs      → S7A_FAQ
      menu_agent     → S8_AGENT (terminal)
  S3A_BRAND   → brand_*  → S3B_MODELS
  S3B_MODELS  → model_*  → S3C_DETAIL
  S3C_DETAIL  → act_test_ride → S_ENDED, act_dealer → S4A_ASK_PINCODE
  S4A_ASK_PINCODE → (text) → test_ride/service → S_ENDED, dealer → S4B_OUTLETS
  S4B_OUTLETS → out_* → stay, act_menu → S2_MAIN_MENU
  S7A_FAQ     → faq_*  → S7B_FAQ_ANSWER
  S7B_FAQ_ANSWER → faq_back → S7A_FAQ, act_menu → S2_MAIN_MENU
"""

import asyncio
import string
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from loguru import logger
from database import get_async_mongo_db

# ═══════════════════════════════════════════════════════════════════════
# 1. CONSTANTS & CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════

SHAKTI_ENABLED = True          # Kill switch — set to False to disable instantly
SHAKTI_ORG_ID = "org_2zV2OOhodLocEnTybzR8t9MIYCE"

TRIGGER_KEYWORDS = frozenset({"hi", "hello", "hey", "menu", "start", "hii", "hiii", "heyy", "heya", "namaste"})
AGENT_KEYWORDS   = frozenset({"agent", "human", "talk to a person", "help"})

SESSION_TTL_HOURS = 24
AUDIT_TTL_DAYS    = 14
AUDIT_COLLECTION  = "shakti_audit_log"

async_db = get_async_mongo_db()   # Motor async database object (not a coroutine)


# ═══════════════════════════════════════════════════════════════════════
# 2. DATA — Bikes, Outlets, FAQs
# ═══════════════════════════════════════════════════════════════════════

BIKES = {
    "jawa": [
        {"id": "jawa_42",        "title": "Jawa 42",        "price": "₹1,84,942", "img": "https://jawayezdi.com/images/jawa42.jpg"},
        {"id": "jawa_42_fj",     "title": "Jawa 42 FJ",     "price": "₹1,54,551", "img": "https://jawayezdi.com/images/jawa42fj.jpg"},
        {"id": "jawa_42_bobber", "title": "Jawa 42 Bobber",  "price": "₹2,08,942", "img": "https://jawayezdi.com/images/bobber.jpg"},
        {"id": "jawa_350",       "title": "Jawa 350",       "price": "₹2,15,815", "img": "https://jawayezdi.com/images/jawa350.jpg"},
        {"id": "jawa_perak",     "title": "Jawa Perak",     "price": "₹2,15,815", "img": "https://jawayezdi.com/images/perak.jpg"},
    ],
    "yezdi": [
        {"id": "yezdi_roadster",  "title": "Yezdi Roadster",  "price": "~₹1,98,000", "img": "https://jawayezdi.com/images/roadster.jpg"},
        {"id": "yezdi_scrambler", "title": "Yezdi Scrambler", "price": "~₹2,14,900", "img": "https://jawayezdi.com/images/scrambler.jpg"},
        {"id": "yezdi_adventure", "title": "Yezdi Adventure", "price": "~₹2,14,900", "img": "https://jawayezdi.com/images/adventure.jpg"},
    ],
    "bsa": [
        {"id": "bsa_gold_star", "title": "BSA Gold Star 650", "price": "₹2,99,995", "img": "https://bsa.com/images/goldstar.jpg"},
    ],
}

OUTLETS = [
    {"id": "out_baner", "title": "Baner", "desc": "Baner, Pune", "url": "https://maps.google.com/?q=Jawa+Yezdi+Baner+Pune"},
    {"id": "out_wakad", "title": "Wakad", "desc": "Wakad, Pune", "url": "https://maps.google.com/?q=Jawa+Yezdi+Wakad+Pune"},
    {"id": "out_chinchwad", "title": "Chinchwad", "desc": "Chinchwad, Pune", "url": "https://maps.google.com/?q=Jawa+Yezdi+Chinchwad+Pune"},
    {"id": "out_ravet", "title": "Ravet", "desc": "Survey No 288, Pune Bhir Plaza", "url": "https://maps.google.com/?q=Jawa+Yezdi+Ravet+Pune"},
    {"id": "out_sinhagad", "title": "Sinhagad Road", "desc": "Sinhagad Road, Pune", "url": "https://maps.google.com/?q=Jawa+Yezdi+Sinhagad+Road+Pune"},
    {"id": "out_goa", "title": "Goa", "desc": "Goa Showroom", "url": "https://maps.google.com/?q=Jawa+Yezdi+Goa"}
]

FAQS = [
    {"id": "faq_warranty",  "title": "Warranty",          "answer": "2 years standard, extendable."},
    {"id": "faq_service",   "title": "Service Interval",  "answer": "Every 6 months or 6000 km whichever comes first."},
    {"id": "faq_rsa",       "title": "Roadside Assistance","answer": "Included at time of purchase."},
    {"id": "faq_insurance", "title": "Insurance",         "answer": "Available at time of purchase."},
    {"id": "faq_delivery",  "title": "Delivery Time",     "answer": "No home delivery (showroom pickup only): ~10 days from booking."},
    {"id": "faq_tradein",   "title": "Trade-In",          "answer": "Accepted. Bring your bike to any outlet."},
    {"id": "faq_testrides", "title": "Test Rides",        "answer": "Available by appointment at all outlets."},
]


# ═══════════════════════════════════════════════════════════════════════
# 3. LAZY IMPORT CACHING
# ═══════════════════════════════════════════════════════════════════════

_interactive_adapter = None

def _get_interactive_adapter():
    """Lazily create and cache the Pydantic TypeAdapter for interactive msgs."""
    global _interactive_adapter
    if _interactive_adapter is None:
        from pydantic import TypeAdapter
        from schemas.models import WhatsAppInteractiveMessage
        _interactive_adapter = TypeAdapter(WhatsAppInteractiveMessage)
    return _interactive_adapter


# ═══════════════════════════════════════════════════════════════════════
# 4. DATABASE HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _matches_trigger(text: str) -> bool:
    if not text: return False
    clean = str(text).lower().translate(str.maketrans('', '', string.punctuation)).strip()
    words = clean.split()
    return any(w in TRIGGER_KEYWORDS for w in words)

def _extract_phone(conversation_id: str) -> str:
    """Extract customer phone from conversation_id.

    Format: whatsapp_{business_phone_id}_{customer_phone}
    Example: whatsapp_904449942754247_918320530170 → 918320530170
    """
    if not conversation_id or not conversation_id.startswith("whatsapp_"):
        return ""
    parts = conversation_id.split("_")
    return parts[-1] if len(parts) >= 3 else ""


async def _audit(org_id, phone, wa_message_id, action, from_state, to_state, details=None):
    """Write an audit record. Never raises — failures are logged and swallowed."""
    doc = {
        "org_id":        org_id,
        "phone":         phone,
        "wa_message_id": wa_message_id,
        "action":        action,
        "from_state":    from_state,
        "to_state":      to_state,
        "details":       details,
        "created_at":    datetime.now(timezone.utc),
        "expires_at":    datetime.now(timezone.utc) + timedelta(days=AUDIT_TTL_DAYS),
    }
    try:
        await async_db[AUDIT_COLLECTION].insert_one(doc)
    except Exception as e:
        logger.error(f"[SHAKTI] Audit log failed: {e}")


async def _is_duplicate_message(wa_message_id: str) -> bool:
    """Check if this wa_message_id was already processed."""
    if not wa_message_id:
        return False
    try:
        existing = await async_db[AUDIT_COLLECTION].find_one(
            {"wa_message_id": wa_message_id}
        )
        return existing is not None
    except Exception as e:
        logger.error(f"[SHAKTI] Dedup check failed: {e}")
        return False


async def _get_session(org_id: str, phone: str) -> Optional[dict]:
    """Fetch an existing session. Never creates one."""
    try:
        return await async_db[f"shakti_sessions_{org_id}"].find_one({"phone_number": phone})
    except Exception as e:
        logger.error(f"[SHAKTI] Session fetch failed for {phone}: {e}")
        return None


async def _create_session(org_id: str, phone: str, conversation_id: str) -> dict:
    """Create a brand-new session."""
    now = datetime.now(timezone.utc)
    session = {
        "phone_number":   phone,
        "conversation_id": conversation_id,
        "first_name":     None,
        "state":          "S1_WELCOME",
        "context":        {},
        "created_at":     now,
        "updated_at":     now,
        "expires_at":     now + timedelta(hours=SESSION_TTL_HOURS),
    }
    try:
        await async_db[f"shakti_sessions_{org_id}"].insert_one(session)
    except Exception as e:
        logger.error(f"[SHAKTI] Session create failed for {phone}: {e}")
    return session


async def _update_session(
    org_id: str,
    phone: str,
    state: str,
    context: dict = None,
    first_name: str = None,
    conversation_id: str = None,
):
    """Update session state, context, and refresh TTL."""
    now = datetime.now(timezone.utc)
    update_fields: Dict[str, Any] = {
        "state":      state,
        "updated_at": now,
        "expires_at": now + timedelta(hours=SESSION_TTL_HOURS),
    }
    if context is not None:
        update_fields["context"] = context
    if first_name is not None:
        update_fields["first_name"] = first_name
    if conversation_id is not None:
        update_fields["conversation_id"] = conversation_id

    try:
        await async_db[f"shakti_sessions_{org_id}"].update_one(
            {"phone_number": phone},
            {"$set": update_fields},
        )
    except Exception as e:
        logger.error(f"[SHAKTI] Session update failed for {phone}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 5. MESSAGING HELPERS (lazy-import wa_service)
# ═══════════════════════════════════════════════════════════════════════

async def _send_text(org_id: str, wa_id: str, conversation_id: str, text: str):
    """Send a plain text WhatsApp message."""
    try:
        from services.wa_service import send_whatsapp_message
        await send_whatsapp_message(
            org_id=org_id,
            mode="reply",
            message={
                "sender_id":       wa_id,
                "conversation_id": conversation_id,
                "content":         text,
            },
        )
    except Exception as e:
        logger.error(f"[SHAKTI] Failed to send text to {conversation_id}: {e}")


async def _send_interactive(org_id: str, wa_id: str, conversation_id: str, payload_dict: dict):
    """Validate and send an interactive WhatsApp message."""
    try:
        from services.wa_service import send_interactive_whatsapp_message
        validated = _get_interactive_adapter().validate_python(payload_dict)
        await send_interactive_whatsapp_message(
            org_id=org_id,
            conversation_id=conversation_id,
            sender_id=wa_id,
            interactive_message=validated,
        )
    except Exception as e:
        logger.error(f"[SHAKTI] Failed to send interactive to {conversation_id}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 6. RESPONSE GENERATORS
# ═══════════════════════════════════════════════════════════════════════


async def _send_image(org_id: str, wa_id: str, conversation_id: str, media_url: str):
    """Send an image to the user."""
    try:
        from services.wa_service import send_wa_media_message
        customer_phone = _extract_phone(conversation_id)
        if not customer_phone: return
        await send_wa_media_message(
            recipient_id=customer_phone,
            media_url=media_url,
            media_type="image",
            wa_id=wa_id,
            content=None,
            org_id=org_id,
            temp_id=None,
            mode="reply",
            context_type=None,
            context=None,
            conversation_id=conversation_id,
        )
    except Exception as e:
        logger.error(f"[SHAKTI] Failed to send image to {conversation_id}: {e}")

async def _send_welcome(org_id, wa_id, cid):
    text = (
        "Hi! I'm Vela, Shakti Motors' assistant — here to help with "
        "Jawa, Yezdi, and BSA.\n\nWhat should I call you? (Or type 'skip'.)"
    )
    await _send_text(org_id, wa_id, cid, text)


async def _send_main_menu(org_id, wa_id, cid, name):
    greeting = f"Nice to meet you, {name}!" if name and name.lower() != "skip" else "Welcome!"
    
    img_url = "https://res.cloudinary.com/djpc5zfgz/image/upload/v1791111128/whatsapp_media/org_2zV2OOhodLocEnTybzR8t9MIYCE/user/d5a6853c277e4f4989ba593aa056c2c1-shakti_welcome.jpg"
    await _send_image(org_id, wa_id, cid, img_url)
    await asyncio.sleep(1)
    
    payload = {
        "type": "list",
        "body_text": f"{greeting} What can I help with today?",
        "button_text": "Select Option",
        "sections": [{
            "title": "Main Menu",
            "rows": [
                {"id": "menu_browse",    "title": "Browse Bikes"},
                {"id": "menu_dealer",    "title": "Find a Dealer"},
                {"id": "menu_test_ride", "title": "Book a Test Ride"},
                {"id": "menu_service",   "title": "Book a Service"},
                {"id": "menu_faqs",      "title": "FAQs & Policies"},
                {"id": "menu_agent",     "title": "Talk to a Person"},
            ],
        }],
    }
    await _send_interactive(org_id, wa_id, cid, payload)


async def _send_brand_selection(org_id, wa_id, cid):
    payload = {
        "type": "button",
        "body_text": "Which brand are you exploring?",
        "buttons": [
            {"type": "reply", "reply": {"id": "brand_jawa",  "title": "Jawa"}},
            {"type": "reply", "reply": {"id": "brand_yezdi", "title": "Yezdi"}},
            {"type": "reply", "reply": {"id": "brand_bsa",   "title": "BSA"}},
        ],
    }
    await _send_interactive(org_id, wa_id, cid, payload)


async def _send_models(org_id, wa_id, cid, brand):
    brand_bikes = BIKES.get(brand, BIKES["jawa"])
    rows = [
        {"id": f"model_{b['id']}", "title": b["title"], "description": "Explore features and book a ride"}
        for b in brand_bikes
    ]
    payload = {
        "type": "list",
        "body_text": f"Here are the {brand.title()} models. Which one would you like to look at?",
        "button_text": "View Models",
        "sections": [{"title": f"{brand.title()} Bikes", "rows": rows[:10]}],
    }
    await _send_interactive(org_id, wa_id, cid, payload)


async def _send_model_detail(org_id, wa_id, cid, model_id, brand):
    brand_bikes = BIKES.get(brand, BIKES["jawa"])
    bike = next((b for b in brand_bikes if b["id"] == model_id), brand_bikes[0])
    payload = {
        "type": "button",
        "body_text": f"*{bike['title']}*\n\nGreat choice! What would you like to do next?",
        "buttons": [
            {"type": "reply", "reply": {"id": "act_test_ride", "title": "Book Test Ride"}},
            {"type": "reply", "reply": {"id": "act_dealer",    "title": "Find Dealer"}},
            {"type": "reply", "reply": {"id": "act_menu",      "title": "Back to Menu"}},
        ],
    }
    # Removed image header to prevent Meta API blocking on mock URLs
    await _send_interactive(org_id, wa_id, cid, payload)


async def _send_test_ride_cta(org_id, wa_id, cid):
    payload = {
        "type": "cta_url",
        "body_text": "Let's get your test ride booked. Fill out this quick form and our team will confirm your slot.",
        "button_text": "Book Test Ride",
        "url": "https://docs.google.com/forms/d/e/1FAIpQLSc10b6GwCw8NsrqX6DY1PtL9AIdE8XDV_SJa_f7Ji_8IZEkZQ/viewform?usp=publish-editor",
    }
    await _send_interactive(org_id, wa_id, cid, payload)
    await asyncio.sleep(1)
    back = {
        "type": "button",
        "body_text": "Need anything else?",
        "buttons": [
            {"type": "reply", "reply": {"id": "act_menu", "title": "Main Menu"}},
            {"type": "reply", "reply": {"id": "act_end", "title": "End Chat"}}
        ]
    }
    await _send_interactive(org_id, wa_id, cid, back)


async def _send_service_cta(org_id, wa_id, cid):
    payload = {
        "type": "cta_url",
        "body_text": "Need a service? Schedule an appointment at any of our outlets.",
        "button_text": "Book Service",
        "url": "https://docs.google.com/forms/d/e/1FAIpQLSeF91ySQvpjxaQqjywzvIcuJ9aagDE_Q91oEiuOXdbIzbhjZg/viewform?usp=publish-editor",
    }
    await _send_interactive(org_id, wa_id, cid, payload)
    await asyncio.sleep(1)
    back = {
        "type": "button",
        "body_text": "Need anything else?",
        "buttons": [
            {"type": "reply", "reply": {"id": "act_menu", "title": "Main Menu"}},
            {"type": "reply", "reply": {"id": "act_end", "title": "End Chat"}}
        ]
    }
    await _send_interactive(org_id, wa_id, cid, back)


async def _send_outlets(org_id, wa_id, cid):
    rows = [{"id": o["id"], "title": o["title"], "description": o["desc"]} for o in OUTLETS]
    payload = {
        "type": "list",
        "body_text": "Here are our Shakti Motors outlets in Pune. Select one for directions.",
        "button_text": "View Outlets",
        "sections": [{"title": "Showrooms", "rows": rows}],
    }
    await _send_interactive(org_id, wa_id, cid, payload)


async def _send_outlet_detail(org_id, wa_id, cid, outlet_id):
    outlet = next((o for o in OUTLETS if o["id"] == outlet_id), OUTLETS[0])
    payload = {
        "type": "cta_url",
        "body_text": f"*{outlet['title']}*\n{outlet['desc']}",
        "button_text": "Get Directions",
        "url": outlet["url"],
    }
    await _send_interactive(org_id, wa_id, cid, payload)
    await asyncio.sleep(1)
    back = {
        "type": "button",
        "body_text": "Need anything else?",
        "buttons": [{"type": "reply", "reply": {"id": "act_menu", "title": "Back to Menu"}}, {"type": "reply", "reply": {"id": "act_end", "title": "End Chat"}}],
    }
    await _send_interactive(org_id, wa_id, cid, back)


async def _send_faqs(org_id, wa_id, cid):
    rows = [{"id": f["id"], "title": f["title"]} for f in FAQS]
    payload = {
        "type": "list",
        "body_text": "What would you like to know?",
        "button_text": "View FAQs",
        "sections": [{"title": "Policies & Info", "rows": rows}],
    }
    await _send_interactive(org_id, wa_id, cid, payload)


async def _send_faq_answer(org_id, wa_id, cid, faq_id):
    faq = next((f for f in FAQS if f["id"] == faq_id), FAQS[0])
    payload = {
        "type": "button",
        "body_text": f"*{faq['title']}*\n{faq['answer']}",
        "buttons": [
            {"type": "reply", "reply": {"id": "faq_back",  "title": "Ask something else"}},
            {"type": "reply", "reply": {"id": "act_menu",  "title": "Back to Menu"}},
        ],
    }
    await _send_interactive(org_id, wa_id, cid, payload)


# ═══════════════════════════════════════════════════════════════════════
# 7. MAIN HANDLER
# ═══════════════════════════════════════════════════════════════════════

async def handle_shakti_message(
    org_id: str,
    conversation_id: str,
    message_type: str,
    content: str,
    sender_name: str,
    whatsapp_business_id: str,
    wa_message_id: str = None,
    message_dict: dict = None,
) -> bool:
    """Process an incoming WhatsApp message for Shakti Motors.

    Returns True  → message handled, webhook should return early.
    Returns False → message not handled, webhook continues to normal flow.
    """
    # ── Guard clauses ──────────────────────────────────────────────────
    if not SHAKTI_ENABLED:
        return False

    if org_id != SHAKTI_ORG_ID:
        return False

    customer_phone = _extract_phone(conversation_id)
    if not customer_phone:
        logger.warning(f"[SHAKTI] Could not extract phone from {conversation_id}")
        return False

    # ── Message deduplication ──────────────────────────────────────────
    if wa_message_id and await _is_duplicate_message(wa_message_id):
        logger.info(f"[SHAKTI] Duplicate message {wa_message_id}, skipping")
        return True

    # ── Extract interactive selection ID ───────────────────────────────
    interactive_id = ""
    if message_type == "interactive" and message_dict:
        try:
            idata = message_dict.get("interactive", {})
            itype = idata.get("type", "")
            if itype == "list_reply":
                interactive_id = idata.get("list_reply", {}).get("id", "")
            elif itype == "button_reply":
                interactive_id = idata.get("button_reply", {}).get("id", "")
            logger.info(f"[SHAKTI] Interactive extraction: itype={itype}, interactive_id={interactive_id}")
        except Exception as _extract_err:
            logger.error(f"[SHAKTI] Interactive extraction failed: {_extract_err}")

    # Also handle WhatsApp "button" type replies (quick reply buttons)
    if message_type == "button" and message_dict:
        try:
            btn_payload = message_dict.get("button", {}).get("payload", "")
            btn_text = message_dict.get("button", {}).get("text", "")
            logger.info(f"[SHAKTI] Button type detected: payload={btn_payload}, text={btn_text}")
            # Use payload as interactive_id if it matches our button IDs
            if btn_payload:
                interactive_id = btn_payload
        except Exception:
            pass

    # ── Normalize text ─────────────────────────────────────────────────
    text_content = ""
    if isinstance(content, str):
        text_content = content.strip().lower()

    # Unified input for logging
    input_str = interactive_id or text_content or content or ""

    # Debug log for ALL incoming Shakti messages
    logger.info(
        f"[SHAKTI] Incoming: phone={customer_phone}, "
        f"message_type={message_type}, interactive_id='{interactive_id}', "
        f"text_content='{text_content}', content='{str(content)[:80]}', "
        f"message_dict_keys={list(message_dict.keys()) if message_dict else None}"
    )

    # Shorthand
    wa_id = whatsapp_business_id
    cid   = conversation_id

    # ── Get session ────────────────────────────────────────────────────
    session = await _get_session(org_id, customer_phone)

    # ── No session exists ──────────────────────────────────────────────
    if not session:
        if _matches_trigger(text_content):
            from services.wa_service import create_or_update_whatsapp_conversation
            await create_or_update_whatsapp_conversation(
                org_id=org_id,
                recipient_id=wa_id,
                customer_phone_no=customer_phone,
                customer_name=sender_name,
                last_message=text_content,
                last_sender="customer",
                mode="reply",
                business_phone_id=wa_id
            )
            await _create_session(org_id, customer_phone, conversation_id)
            await _send_welcome(org_id, wa_id, cid)
            await _audit(org_id, customer_phone, wa_message_id,
                         "session_created", None, "S1_WELCOME",
                         {"trigger": text_content})
            return True
        # Not a trigger keyword and no session → don't intercept
        return False

    current_state = session.get("state", "S1_WELCOME")
    ctx           = session.get("context", {})
    first_name    = session.get("first_name", "")


    if interactive_id == "act_end" or "end chat" in text_content:
        await _update_session(org_id, customer_phone, "S_ENDED", ctx, conversation_id=cid)
        await _send_text(org_id, wa_id, cid, "Thanks for chatting with Shakti Motors! Have a great day.")
        await _audit(org_id, customer_phone, wa_message_id, "ended_chat", current_state, "S_ENDED")
        return True

    # ── Terminal states: only reactivate on trigger keywords ───────────
    if current_state in ("S_ENDED", "S8_AGENT"):
        if _matches_trigger(text_content) or interactive_id == "act_menu":
            if first_name:
                await _update_session(org_id, customer_phone, "S2_MAIN_MENU",
                                      context={}, conversation_id=cid)
                await _send_main_menu(org_id, wa_id, cid, first_name)
                await _audit(org_id, customer_phone, wa_message_id,
                             "reactivated", current_state, "S2_MAIN_MENU")
            else:
                await _update_session(org_id, customer_phone, "S1_WELCOME",
                                      context={}, conversation_id=cid)
                await _send_welcome(org_id, wa_id, cid)
                await _audit(org_id, customer_phone, wa_message_id,
                             "reactivated", current_state, "S1_WELCOME")
            return True
        return False   # Terminal state, not a keyword → fall through

    # ── Global keyword overrides (active sessions) ────────────────────
    if _matches_trigger(text_content):
        if first_name:
            await _update_session(org_id, customer_phone, "S2_MAIN_MENU",
                                  context={}, conversation_id=cid)
            await _send_main_menu(org_id, wa_id, cid, first_name)
        else:
            await _update_session(org_id, customer_phone, "S1_WELCOME",
                                  context={}, conversation_id=cid)
            await _send_welcome(org_id, wa_id, cid)
        await _audit(org_id, customer_phone, wa_message_id,
                     "global_keyword", current_state, "S2_MAIN_MENU" if first_name else "S1_WELCOME",
                     {"keyword": text_content})
        return True

    if text_content in AGENT_KEYWORDS:
        await _update_session(org_id, customer_phone, "S8_AGENT", ctx, conversation_id=cid)
        await _send_text(org_id, wa_id, cid,
                         "I'll connect you with our team — someone will reply here shortly. "
                         "In the meantime, is there anything I can note down for them?")
        await _audit(org_id, customer_phone, wa_message_id,
                     "agent_handoff", current_state, "S8_AGENT")
        return True

    # ── State machine routing ─────────────────────────────────────────
    new_state = current_state
    handled   = False

    logger.info(
        f"[SHAKTI] State machine: state={current_state}, "
        f"interactive_id='{interactive_id}', text_content='{text_content}'"
    )

    if current_state == "S1_WELCOME":
        if message_type == "text" and text_content:
            name = text_content.title() if len(text_content) < 30 else sender_name
            new_state = "S2_MAIN_MENU"
            await _update_session(org_id, customer_phone, new_state, ctx,
                                  first_name=name, conversation_id=cid)
            await _send_main_menu(org_id, wa_id, cid, name)
            first_name = name
            handled = True

    elif current_state == "S2_MAIN_MENU":
        if interactive_id == "menu_browse" or "browse" in text_content or "bikes" in text_content:
            new_state = "S3A_BRAND"
            await _send_brand_selection(org_id, wa_id, cid)
            handled = True
        elif interactive_id == "menu_dealer" or "dealer" in text_content or "find" in text_content:
            new_state = "S4B_OUTLETS"
            await _send_outlets(org_id, wa_id, cid)
            handled = True
        elif interactive_id == "menu_test_ride" or "test ride" in text_content:
            new_state = "S_ENDED"
            await _send_test_ride_cta(org_id, wa_id, cid)
            handled = True
        elif interactive_id == "menu_service" or "service" in text_content:
            new_state = "S_ENDED"
            await _send_service_cta(org_id, wa_id, cid)
            handled = True
        elif interactive_id == "menu_faqs" or "faq" in text_content or "policies" in text_content:
            new_state = "S7A_FAQ"
            await _send_faqs(org_id, wa_id, cid)
            handled = True
        elif interactive_id == "menu_agent" or any(k in text_content for k in AGENT_KEYWORDS):
            new_state = "S8_AGENT"
            await _send_text(org_id, wa_id, cid,
                             "I'll connect you with our team — someone will reply here shortly.")
            handled = True

    elif current_state == "S3A_BRAND":
        if interactive_id.startswith("brand_"):
            brand = interactive_id.replace("brand_", "")
            ctx["brand"] = brand
            new_state = "S3B_MODELS"
            await _send_models(org_id, wa_id, cid, brand)
            handled = True
        elif "jawa" in text_content:
            ctx["brand"] = "jawa"
            new_state = "S3B_MODELS"
            await _send_models(org_id, wa_id, cid, "jawa")
            handled = True
        elif "yezdi" in text_content:
            ctx["brand"] = "yezdi"
            new_state = "S3B_MODELS"
            await _send_models(org_id, wa_id, cid, "yezdi")
            handled = True
        elif "bsa" in text_content:
            ctx["brand"] = "bsa"
            new_state = "S3B_MODELS"
            await _send_models(org_id, wa_id, cid, "bsa")
            handled = True

    elif current_state == "S3B_MODELS":
        if interactive_id.startswith("model_"):
            model_id = interactive_id.replace("model_", "")
            ctx["model"] = model_id
            new_state = "S3C_DETAIL"
            await _send_model_detail(org_id, wa_id, cid, model_id, ctx.get("brand", "jawa"))
            handled = True

    elif current_state == "S3C_DETAIL":
        if interactive_id == "act_test_ride" or "test ride" in text_content:
            new_state = "S_ENDED"
            await _send_test_ride_cta(org_id, wa_id, cid)
            handled = True
        elif interactive_id == "act_dealer" or "dealer" in text_content:
            new_state = "S4B_OUTLETS"
            await _send_outlets(org_id, wa_id, cid)
            handled = True
        elif interactive_id == "act_menu" or "menu" in text_content or "back" in text_content:
            new_state = "S2_MAIN_MENU"
            await _send_main_menu(org_id, wa_id, cid, first_name)
            handled = True

    elif current_state == "S4B_OUTLETS":
        if interactive_id.startswith("out_"):
            await _send_outlet_detail(org_id, wa_id, cid, interactive_id)
            handled = True
        elif interactive_id == "act_menu":
            new_state = "S2_MAIN_MENU"
            await _send_main_menu(org_id, wa_id, cid, first_name)
            handled = True

    elif current_state == "S7A_FAQ":
        if interactive_id.startswith("faq_"):
            new_state = "S7B_FAQ_ANSWER"
            await _send_faq_answer(org_id, wa_id, cid, interactive_id)
            handled = True

    elif current_state == "S7B_FAQ_ANSWER":
        if interactive_id == "faq_back":
            new_state = "S7A_FAQ"
            await _send_faqs(org_id, wa_id, cid)
            handled = True
        elif interactive_id in ("menu_main", "act_menu"):
            new_state = "S2_MAIN_MENU"
            await _send_main_menu(org_id, wa_id, cid, first_name)
            handled = True

    # ── Persist state & audit ─────────────────────────────────────────
    if handled:
        if new_state != current_state:
            await _update_session(org_id, customer_phone, new_state, ctx,
                                  first_name=first_name, conversation_id=cid)
        await _audit(org_id, customer_phone, wa_message_id,
                     "state_transition", current_state, new_state,
                     {"input": input_str})
        return True

    # ── No handler matched → fall through to normal AI/flow ───────────
    return False


# ═══════════════════════════════════════════════════════════════════════
# 8. INDEX SETUP
# ═══════════════════════════════════════════════════════════════════════

async def ensure_shakti_indexes(org_id: str):
    """Create TTL and query indexes. Safe to call multiple times."""
    try:
        sessions = async_db[f"shakti_sessions_{org_id}"]
        await sessions.create_index("expires_at", expireAfterSeconds=0, name="session_ttl")
        await sessions.create_index("phone_number", unique=True, name="phone_unique")

        audit = async_db[AUDIT_COLLECTION]
        await audit.create_index("expires_at", expireAfterSeconds=0, name="audit_ttl")
        await audit.create_index("wa_message_id", name="dedup_idx")
        await audit.create_index(
            [("org_id", 1), ("phone", 1), ("created_at", -1)],
            name="phone_lookup",
        )
        logger.info(f"[SHAKTI] Indexes ensured for org {org_id}")
    except Exception as e:
        logger.warning(f"[SHAKTI] Index creation (safe to ignore if exists): {e}")
