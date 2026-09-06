# Built-in Modules
import asyncio, time, json, hmac, hashlib
from fastapi import APIRouter, HTTPException, Response
from datetime import datetime, timezone, timedelta
from fastapi import Request, Header, BackgroundTasks
from fastapi.responses import PlainTextResponse
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
import httpx

# Internal Modules 
from core.managers import manager
from services.automation_service import execute_whatsapp_flow
from services.pma_chatbot import get_vedic_maths_books_message, process_onboarding_if_needed, send_to_pma_chatbot
from services.cloudinary_service import upload_and_send_whatsapp_media_background
from services.followup_service import handle_followup_customer_reply
from services.contacts_service import store_contact_background
from services.payment_service import build_idempotency_key, process_cashfree_event, verify_cashfree_signature
from services.heidelai_bot import send_to_chatbot
from services.agent_service import send_to_agent
from services.sales_action_service import mark_sales_action_delivered
from services.ai_policy_service import ai_access_state, is_effective_ai_enabled
from services.ai_reply_guard import AIProviderUnavailable, log_ai_failure
from services.pricing_decision_service import get_pricing_settings
from services.pricing_service import is_price_signal, is_pricing_enabled, observe_customer_message, pricing_failure_intercept
from services.wa_service import *
from services.ig_service import *
from services.utils import normalize_phone
from schemas.models import ActionButton, InteractiveUrlButtonMessage, ReplyButton, InteractiveReplyButtonMessage
from schemas.models import MessageRole
from database import get_mongo_db
from config.settings import (
    VERIFY_TOKEN, APP_SECRET, HEIDELAI_ORG_WA_ID, DEV_WEBHOOK_URL, TEST_WEBHOOK_URL, ENVIRONMENT, PARIJAT_ACADEMY_ORG_WA_ID, 
    LEE_DEALER_ORG_WA_ID, KARAN_ORG_WA_ID, INSTAGRAM_APP_SECRET, GIGA_ORG_WA_ID,
    EGENIE_WA_ID, EGENIE_IG_ID, SAHIL_WA_ID, SAHIL_IG_ID, KOMAL_ORG_WA_ID, HEIDELAI_ORG_IG_ID)
from core.services import services
from loguru import logger
from services.webhook_service import calculate_whatsapp_reply_window
from services.automation_trigger_handler import check_and_trigger_automations
from services.push_notification_service import schedule_customer_message_push
from services.whatsapp_call_history import (
    call_lifecycle_fields,
    meta_datetime,
    provider_lifecycle_filter,
)
from services.intent_queue_worker import enqueue_intent_check
from services.shopify_service import (
    normalize_whatsapp_delivery_error,
    record_whatsapp_preference,
    record_whatsapp_provider_block,
    update_shopify_reminder_delivery,
)

router = APIRouter(prefix="/webhook", tags=["Webhooks"])
db = get_mongo_db()

DEV_WHATSAPP_WABA_IDS = {
    value
    for value in (SAHIL_WA_ID, EGENIE_WA_ID)
    if value
}
DEV_WHATSAPP_PHONE_IDS = {"1248072875064563"}


async def _best_effort_typing(
    whatsapp_id: str, message_id: str, conversation_id: str
) -> None:
    try:
        await read_message_and_typing_indicator(
            whatsapp_id=whatsapp_id,
            message_id=message_id,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        logger.warning("Could not send WhatsApp typing indicator: {}", exc)


async def _propagate_whatsapp_delivery_status(
    org_id: str,
    message_id: str,
    status_type: str,
    status_payload: dict,
    delivery_error: dict | None,
    include_shopify: bool,
) -> None:
    if status_type == "failed":
        try:
            await asyncio.to_thread(
                record_whatsapp_provider_block,
                org_id,
                status_payload.get("recipient_id"),
                status_payload,
            )
        except Exception as exc:
            logger.debug("WhatsApp provider block propagation skipped for {}: {}", message_id, type(exc).__name__)
    if include_shopify:
        try:
            await update_shopify_reminder_delivery(org_id, message_id, status_type, delivery_error)
        except Exception as exc:
            logger.debug("Shopify reminder delivery propagation skipped for {}: {}", message_id, type(exc).__name__)
    try:
        from services.sales_offer_service import update_offer_delivery

        await update_offer_delivery(org_id, message_id, status_type)
    except Exception as exc:
        logger.debug("Offer delivery propagation skipped for {}: {}", message_id, type(exc).__name__)
    try:
        from services.pricing_service import update_ai_pricing_delivery

        await update_ai_pricing_delivery(org_id, message_id, status_type)
    except Exception as exc:
        logger.debug("AI price delivery propagation skipped for {}: {}", message_id, type(exc).__name__)
    try:
        from services.google_sheet_automation import update_sheet_sent_status

        await update_sheet_sent_status(org_id=org_id, message_id=message_id, status=status_type)
    except Exception as exc:
        logger.debug("Sheet status propagation skipped for {}: {}", message_id, type(exc).__name__)


def _log_product_reply_latency(
    org_id: str,
    conversation_id: str,
    chatbot_response: dict,
    send_result: dict,
    started_at: float,
    send_started_at: float,
    received_at: datetime | None = None,
    send_started_wall: datetime | None = None,
) -> None:
    if send_result.get("outbound_replay"):
        return
    response_class = chatbot_response.get("response_class")
    if response_class not in {"product_match", "product_alternative"}:
        return
    agent_timings = chatbot_response.get("product_timings") or {}
    meta_timings = send_result.get("product_timings") or {}
    first_meta_ms = meta_timings.get("first_meta_accepted_ms")
    if not isinstance(first_meta_ms, int):
        return
    if isinstance(received_at, datetime) and isinstance(send_started_wall, datetime):
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        if send_started_wall.tzinfo is None:
            send_started_wall = send_started_wall.replace(tzinfo=timezone.utc)
        total_to_first_meta_ms = max(0, round(
            (send_started_wall - received_at).total_seconds() * 1000
        ) + first_meta_ms)
    else:
        total_to_first_meta_ms = round((send_started_at - started_at) * 1000) + first_meta_ms
    logger.info(
        "Product reply accepted org={} response_class={} conversation=...{} "
        "Math={}ms Meta={}ms total={}ms",
        org_id,
        response_class,
        conversation_id[-6:],
        agent_timings.get("math_ms"),
        first_meta_ms,
        total_to_first_meta_ms,
    )
async def pricing_runtime_mode(org_id: str) -> str:
    """Read rollout mode; retain the legacy allowlist as an outage fallback."""
    try:
        mode = str((await get_pricing_settings(org_id)).get("mode") or "off")
        return mode if mode in {"off", "shadow", "review"} else "off"
    except Exception as exc:
        fallback = "review" if is_pricing_enabled(org_id) else "off"
        logger.error(
            f"Pricing settings lookup failed for org={org_id}; using {fallback} fallback: {exc}"
        )
        return fallback


def normalize_whatsapp_message_content(message: dict) -> str:
    """Return content for the current message without relying on loop state."""
    message_type = str(message.get("type") or "")
    if message_type == "text":
        return str((message.get("text") or {}).get("body") or "")
    if message_type in {"image", "video", "document", "audio"}:
        media = message.get(message_type) or {}
        caption = str(media.get("caption") or "").strip()
        if caption:
            return caption
        article = "an" if message_type in {"image", "audio"} else "a"
        return f"Shared {article} {message_type}"
    if message_type == "location":
        return "Shared a location"
    if message_type == "button":
        return f"[Button: {(message.get('button') or {}).get('payload', 'Unknown')}]"
    if message_type == "order":
        return "Shared an order"
    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get(interactive.get("type")) or {}
        return str(reply.get("title") or reply.get("id") or "Interactive reply")
    if message_type == "unsupported":
        return "[Unsupported message type]"
    return f"[Unsupported message type: {message_type or 'unknown'}]"


async def send_pricing_intercept_reply(
    pricing_result: dict,
    *,
    org_id: str,
    conversation_id: str,
    whatsapp_business_id: str,
    customer_phone_no: str,
    customer_name: str,
    source_message_id: str,
) -> bool:
    """Consume one buyer holding reply; supplier intercepts intentionally stay silent."""
    if not pricing_result.get("intercept"):
        return False
    reply = str(pricing_result.get("reply") or "").strip()
    if not reply:
        return True
    await send_whatsapp_message(
        {
            "conversation_id": conversation_id,
            "content": reply,
            "sender_id": whatsapp_business_id,
            "customer_name": customer_name,
            "recipient_override": customer_phone_no,
        },
        org_id,
        "reply",
        temp_id=f"pricing-hold:{source_message_id}",
        sender_name="Pricing Intelligence",
    )
    return True


def _apply_call_lifecycle(collection, org_id: str, call_id: str, existing: dict, **kwargs) -> dict:
    current = existing
    for _ in range(2):
        fields = call_lifecycle_fields(current, **kwargs)
        provider_time = fields.get("provider_updated_at")
        if not provider_time:
            return current
        result = collection.update_one(
            provider_lifecycle_filter(org_id, call_id, provider_time, fields["state"]),
            {"$set": fields},
        )
        current = collection.find_one({"call_id": call_id, "org_id": org_id}) or current
        if result.matched_count:
            return current
    return current

async def forward_webhook_to_dev(body: bytes, headers: dict) -> bool:
    """
    Forwards the raw webhook body and necessary headers to the dev environment.
    """
    if not DEV_WEBHOOK_URL:
        logger.error("Cannot forward webhook to Dev: DEV_WEBHOOK_URL is not configured")
        return False

    async with httpx.AsyncClient() as client:
        try:
            # We use `content=body` to send the exact raw bytes, 
            # preserving the integrity of the payload for signature validation.
            response = await client.post(DEV_WEBHOOK_URL, content=body, headers=headers)
            response.raise_for_status()
            logger.info(f"Successfully replicated webhook to Dev. Status: {response.status_code}")
            return True
        except Exception as e:
            logger.error(f"Failed to forward webhook to Dev: {e}")
            return False
            
async def forward_webhook_to_test(body: bytes, headers: dict):
    """
    Forwards the raw webhook body and necessary headers to the test environment.
    """
    async with httpx.AsyncClient() as client:
        try:
            # We use `content=body` to send the exact raw bytes, 
            # preserving the integrity of the payload for signature validation.
            response = await client.post(TEST_WEBHOOK_URL, content=body, headers=headers)
            response.raise_for_status()
            logger.info(f"Successfully replicated webhook to Test. Status: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to forward webhook to Test: {e}")

def validate_signature(raw_body: bytes, signature: str) -> bool:
    try:
        payload = json.loads(raw_body)
        platform = payload.get("object", "")
    except Exception:
        return False

    if platform == "instagram":
        secret = INSTAGRAM_APP_SECRET
    else:
        secret = APP_SECRET  # whatsapp + anything else

    expected = hmac.new(
        key=secret.encode('utf-8'),
        msg=raw_body,
        digestmod=hashlib.sha256
    ).hexdigest()

    our_sig = f"sha256={expected}"

    if not hmac.compare_digest(our_sig, signature):
        logger.error(f"Webhook signature mismatch for {platform or 'unknown'}")
        return False

    return True


# Middleware or decorator to validate signature for POST requests
async def signature_required(request: Request, x_hub_signature_256: str = Header(None)):
    # Get the raw request payload bytes.
    body = await request.body() 

    if not x_hub_signature_256:
        raise HTTPException(status_code=403, detail="Signature missing")

    # Pass the raw `body` directly into the validator
    if not validate_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

    return True

def remove_expired_message_ids():
    current_time = time.time()
    expired_ids = []

    # Iterate through the dictionary to find expired message IDs
    for message_id, timestamp in services.processed_message_ids.items():
        if current_time - timestamp > services.EXPIRY_TIME_SECONDS:
            expired_ids.append(message_id)

    # Remove the expired message IDs
    for message_id in expired_ids:
        del services.processed_message_ids[message_id]

async def check_and_handle_dealer_reply(
    org_id: str,
    customer_phone_no: str,
    content: str,
    message_context: dict | None,
    create_escalation: bool = True,
    source_message_id: str | None = None,
) -> bool:
    """Delegate to oos_service which handles matching, logging to sheet, and escalation."""
    from services.oos_service import handle_dealer_reply
    try:
        return await handle_dealer_reply(
            org_id,
            customer_phone_no,
            content,
            message_context,
            create_escalation=create_escalation,
            source_message_id=source_message_id,
        )
    except Exception as e:
        logger.error(f"Error in check_and_handle_dealer_reply: {e}")
        return False


async def _handle_ai_provider_failure(
    error: AIProviderUnavailable,
    *,
    org_id: str,
    conversation_id: str,
    platform: str,
    customer_name: str | None,
    customer_number: str,
    source_message_id: str | None,
    state_already_checked: bool = False,
    resume_pending: bool = False,
) -> None:
    log_ai_failure(error, org_id=org_id, conversation_id=conversation_id)
    if error.category in {"sales_action_in_progress", "outbound_in_progress"}:
        if platform == "whatsapp" and source_message_id:
            services.processed_message_ids.pop(source_message_id, None)
        raise HTTPException(status_code=503, detail="AI reply is still processing")
    if error.category == "quota_exhausted":
        await _send_quota_handoff(org_id=org_id, conversation_id=conversation_id, platform=platform, customer_number=customer_number, customer_name=customer_name)
        return
    delivery_verification_required = error.category in {
        "delivery_unknown", "delivery_sync_failed", "sales_action_delivery_conflict",
    }
    delivery_failure_required = error.category == "delivery_error"
    legacy_action_review_required = error.category == "legacy_sales_action_requires_review"
    sales_action_review_required = error.category == "sales_action_review_required"
    manual_review_required = (
        delivery_verification_required
        or delivery_failure_required
        or legacy_action_review_required
        or sales_action_review_required
    )
    if not error.escalation_required:
        return
    if (
        not manual_review_required
        and not state_already_checked
        and not await is_effective_ai_enabled(org_id, conversation_id)
    ):
        return
    failure_kind = (
        "delivery_verification"
        if delivery_verification_required
        else "delivery_failed"
        if delivery_failure_required
        else "legacy_sales_action_review"
        if legacy_action_review_required
        else "sales_action_review"
        if sales_action_review_required
        else "ai_provider_unavailable"
    )

    def mark_whatsapp_retry_pending() -> None:
        if platform == "whatsapp" and source_message_id:
            try:
                result = db[f"messages_{org_id}"].update_one(
                    {"message_id": source_message_id},
                    {
                        "$set": {
                            "ai_failure_pending": True,
                            "ai_failure_category": error.category,
                            "ai_failure_status": error.upstream_status,
                        }
                    },
                )
                if not result.matched_count:
                    raise RuntimeError("AI failure retry state could not be persisted")
            finally:
                services.processed_message_ids.pop(source_message_id, None)

    now = datetime.now(timezone.utc)
    dedupe_id = "ai-provider:" + hashlib.sha256(
        f"{org_id}\0{platform}\0{conversation_id}\0{failure_kind}".encode()
    ).hexdigest()[:32]
    escalation_id = f"{dedupe_id}:{int(now.timestamp())}"
    try:
        try:
            marker = db.ai_escalation_dedup.find_one_and_update(
                {
                    "_id": dedupe_id,
                    "$or": [
                        {"expires_at": {"$lte": now}},
                        {"expires_at": {"$exists": False}},
                    ],
                },
                {
                    "$set": {
                        "org_id": org_id,
                        "escalation_id": escalation_id,
                        "expires_at": now + timedelta(hours=1),
                    }
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            marker = db.ai_escalation_dedup.find_one({"_id": dedupe_id})
    except Exception:
        mark_whatsapp_retry_pending()
        raise
    if not marker or not marker.get("escalation_id"):
        mark_whatsapp_retry_pending()
        raise RuntimeError("AI escalation deduplication could not be persisted")
    escalation_id = marker["escalation_id"]
    try:
        for _ in range(2):
            renewed = db.ai_escalation_dedup.update_one(
                {"_id": dedupe_id, "escalation_id": escalation_id},
                {"$max": {"expires_at": now + timedelta(hours=1)}},
            )
            if renewed.matched_count:
                break
            marker = db.ai_escalation_dedup.find_one({"_id": dedupe_id})
            if not marker or not marker.get("escalation_id"):
                raise RuntimeError("AI escalation marker disappeared during renewal")
            escalation_id = marker["escalation_id"]
        else:
            raise RuntimeError("AI escalation marker changed repeatedly during renewal")
    except Exception:
        mark_whatsapp_retry_pending()
        raise
    try:
        from services.team_service import create_ai_escalation

        if delivery_verification_required:
            escalation_text = (
                "AI delivery could not be confirmed. Verify the provider and conversation "
                "status before sending anything again."
            )
            escalation_action = "Verify delivery before any resend"
        elif delivery_failure_required:
            escalation_text = (
                "The AI reply was rejected by the messaging provider. Open this "
                "conversation and respond manually."
            )
            escalation_action = "Open the conversation and reply"
        elif legacy_action_review_required:
            escalation_text = (
                "A saved legacy sales action has no immutable input snapshot. "
                "Review the customer request before completing it."
            )
            escalation_action = "Review the saved sales request"
        elif sales_action_review_required:
            escalation_text = (
                "A sales action could not be completed safely. Review the saved "
                "customer request before continuing."
            )
            escalation_action = "Review the failed sales action"
        else:
            escalation_text = "AI could not prepare a reply. Open this conversation and respond."
            escalation_action = "Open the conversation and reply"
        await create_ai_escalation(org_id, {
            "id": escalation_id,
            "user_name": customer_name or "Customer",
            "customer_number": customer_number,
            "customer_text": escalation_text,
            "inferred_intent": (
                "AI delivery requires verification"
                if delivery_verification_required
                else "AI provider unavailable"
            ),
            "priority": "high",
            "kind": "actionable",
            "role": "system",
            "sender_name": "AI Assistant",
            "metadata": {
                "reason": failure_kind,
                "failure_category": error.category,
                "conversation_id": conversation_id,
                "message_id": source_message_id,
                "platform": platform,
                "action": escalation_action,
            },
        })
    except Exception as exc:
        try:
            persisted = db[f"escalations_{org_id}"].find_one({"id": escalation_id}, {"_id": 1})
        except Exception:
            persisted = None
        if not persisted:
            mark_whatsapp_retry_pending()
            raise exc
        logger.error(
            "AI escalation persisted but live broadcast failed org={} conversation=...{}",
            org_id,
            conversation_id[-6:],
        )
    if platform == "whatsapp" and source_message_id:
        result = db[f"messages_{org_id}"].update_one(
            {
                "message_id": source_message_id,
                **({"ai_failure_pending": True} if resume_pending else {}),
            },
            {
                "$set": {"ai_failure_processed_at": datetime.now(timezone.utc)},
                "$unset": {
                    "ai_failure_pending": "",
                    "ai_failure_category": "",
                    "ai_failure_status": "",
                },
            },
        )
        if resume_pending and not result.matched_count:
            completed = db[f"messages_{org_id}"].find_one(
                {
                    "message_id": source_message_id,
                    "ai_failure_pending": {"$exists": False},
                    "ai_failure_processed_at": {"$exists": True},
                },
                {"_id": 1},
            )
            if not completed:
                raise RuntimeError("AI failure retry state changed before completion")


async def _finalize_whatsapp_sales_action(
    *, org_id: str, conversation_id: str, action: dict | None,
    send_result: dict, customer_name: str | None, customer_number: str,
    source_message_id: str,
) -> None:
    action_job_id = action.get("job_id") if action else None
    if send_result.get("sales_action_job_id") != action_job_id:
        logger.error(
            "Sales action reply did not own delivered WhatsApp message org={} conversation=...{}",
            org_id,
            conversation_id[-6:],
        )
        await _handle_ai_provider_failure(
            AIProviderUnavailable("sales_action_delivery_conflict"),
            org_id=org_id,
            conversation_id=conversation_id,
            platform="whatsapp",
            customer_name=customer_name,
            customer_number=customer_number,
            source_message_id=source_message_id,
        )
        return
    if not action:
        return
    try:
        await mark_sales_action_delivered(org_id, action, send_result)
    except Exception as exc:
        services.processed_message_ids.pop(source_message_id, None)
        raise HTTPException(
            status_code=503,
            detail="Sales action delivery is still being finalized",
        ) from exc


async def _send_quota_handoff(
    *,
    org_id: str,
    conversation_id: str,
    platform: str,
    customer_number: str,
    customer_name: str | None,
) -> bool:
    if await ai_access_state(org_id, conversation_id) != "quota_exhausted":
        return False
    from services.entitlement_service import period_window

    period, _ = period_window()
    marker_id = hashlib.sha256(f"{org_id}\0{conversation_id}\0{period}\0quota-handoff".encode()).hexdigest()
    now = datetime.now(timezone.utc)
    marker = db.ai_quota_handoffs.update_one(
        {"_id": marker_id},
        {"$setOnInsert": {"org_id": org_id, "conversation_id": conversation_id, "period": period, "status": "sending", "created_at": now}},
        upsert=True,
    )
    if marker.upserted_id is None:
        return True
    text = "AI limit reached. A team member will reply shortly."
    try:
        if platform == "whatsapp":
            organization = db.organizations.find_one({"org_id": org_id}, {"wa_id": 1}) or {}
            if not organization.get("wa_id"):
                raise RuntimeError("WhatsApp connection not found")
            await send_whatsapp_message(
                {"conversation_id": conversation_id, "content": text, "sender_id": organization["wa_id"], "customer_name": customer_name, "recipient_override": customer_number},
                org_id,
                "reply",
                temp_id=f"quota-handoff:{period}:{conversation_id}",
                sender_name="HeidelAI",
            )
        else:
            organization = db.organizations.find_one({"org_id": org_id}, {"ig_id": 1}) or {}
            if not organization.get("ig_id"):
                raise RuntimeError("Instagram connection not found")
            await send_ig_message(
                id=customer_number,
                message_text=text,
                instagram_id=organization["ig_id"],
                mode="reply",
                org_id=org_id,
                temp_id=f"quota-handoff:{period}:{conversation_id}",
                conversation_id=conversation_id,
                sender_name="HeidelAI",
            )
        db.ai_quota_handoffs.update_one({"_id": marker_id}, {"$set": {"status": "sent", "sent_at": datetime.now(timezone.utc)}})
    except Exception:
        db.ai_quota_handoffs.delete_one({"_id": marker_id, "status": "sending"})
        raise
    return True

@router.get("")
async def verify_webhook(request: Request):
    # This is for the webhook verification (GET request)
    args = request.query_params
    if args.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(content=str(args.get("hub.challenge")))
    return {"status": "Verification failed"}

# TODO: add caching at relevant spots in webhook
@router.post("")
async def handle_webhook(request: Request, x_hub_signature_256: str = Header(None), background_tasks: BackgroundTasks = None):
    """
    This weebhook accepts POST requests from META with a valid signature only
    """
    await signature_required(request, x_hub_signature_256)

    logger.success("Signature validated successfully.")

    # First, validate the signature
    body = await request.body()

    # ── Extract account ID and object type ──────────────────────────────────
    if ENVIRONMENT == "production":
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.error("Webhook received non-JSON body")
            return Response(status_code=400)

        object_type: str = payload.get("object", "")
        account_id: str | None = None

        try:
            account_id = payload["entry"][0]["id"]
        except (KeyError, IndexError):
            logger.warning("Could not extract entry[0].id from webhook payload")
        # Merge note (dev x main, both routing this the same way in the end):
        # keeps dev's null-safe extraction (a webhook payload with an
        # explicit "value": null would crash main's plain .get("value", {})
        # chain) and main's single combined dispatch point (avoids building
        # forward_headers twice for what is otherwise identical logic).
        phone_number_ids = {
            str(((change.get("value") or {}).get("metadata") or {}).get("phone_number_id") or "").strip()
            for entry in payload.get("entry", [])
            for change in entry.get("changes", [])
        }
        phone_number_ids.discard("")

        should_forward_whatsapp = (
            object_type == "whatsapp_business_account"
            and (account_id in DEV_WHATSAPP_WABA_IDS or bool(phone_number_ids & DEV_WHATSAPP_PHONE_IDS))
        )
        should_forward_instagram = (
            object_type == "instagram"
            and account_id in {SAHIL_IG_ID, EGENIE_IG_ID}
        )

        if should_forward_whatsapp or should_forward_instagram:
            logger.info(
                f"Webhook matched development routing: WABA={account_id}, "
                f"phone_number_ids={sorted(phone_number_ids)}. Forwarding."
            )
            forward_headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() in {"content-type", "x-hub-signature", "x-hub-signature-256"}
            }
            if should_forward_whatsapp:
                if not await forward_webhook_to_dev(body=body, headers=forward_headers):
                    return Response(status_code=503)
            else:
                background_tasks.add_task(forward_webhook_to_dev, body=body, headers=forward_headers)
            return Response(status_code=200)

    payload_str = body.decode('utf-8')

    # Now parse the payload
    payload = json.loads(payload_str)

    # Remove expired message IDs
    remove_expired_message_ids()

    is_call_webhook = any(
        change.get("field") == "calls"
        for entry in payload.get("entry", [])
        for change in entry.get("changes", [])
    )
    logger.info("Incoming WhatsApp calling webhook" if is_call_webhook else "Incoming messaging webhook")

    if payload["object"] == "whatsapp_business_account":
        if "entry" in payload and len(payload["entry"]) > 0:
            for entry in payload["entry"]:
                whatsapp_business_id = entry.get("id")
                changes = entry.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    
                    # Validate whatsapp_business_id exists
                    if not whatsapp_business_id:
                        logger.warning("Missing WhatsApp business ID in webhook payload")
                        continue
                    
                    metadata_phone_id = str(
                        (value.get("metadata") or {}).get("phone_number_id") or ""
                    ).strip()
                    connection = await asyncio.to_thread(
                        resolve_active_whatsapp_connection,
                        metadata_phone_id,
                        whatsapp_business_id,
                    )
                    
                    if not connection:
                        logger.error(
                            "No unambiguous active WhatsApp connection for "
                            f"WABA={whatsapp_business_id}, phone_number_id={metadata_phone_id or 'missing'}"
                        )
                        if await asyncio.to_thread(
                            active_whatsapp_connection_candidates,
                            whatsapp_business_id,
                        ):
                            raise HTTPException(
                                status_code=503,
                                detail="WhatsApp connection metadata is temporarily inconsistent",
                            )
                        continue

                    business_phone_id = str(
                        connection.get("phone_id") or metadata_phone_id or ""
                    ).strip() or None
                    business_phone = connection.get("phone")

                    # Calling uses the same webhook URL, but its statuses are not message receipts.
                    if change.get("field") == "calls":
                        org_id = connection.get("org_id")
                        contacts = value.get("contacts") or []
                        contact = contacts[0] if contacts else {}
                        contact_name = contact.get("profile", {}).get("name") or "WhatsApp customer"
                        contact_phone = contact.get("wa_id")
                        calls_collection = db.whatsapp_calls

                        for call in value.get("calls") or []:
                            call_id = call.get("id")
                            if not call_id:
                                continue
                            existing = await asyncio.to_thread(
                                calls_collection.find_one,
                                {"call_id": call_id, "org_id": org_id},
                            ) or {}
                            session = call.get("session")
                            session_type = session.get("sdp_type") if isinstance(session, dict) else None
                            direction = call.get("direction") or existing.get("direction") or (
                                "USER_INITIATED" if session_type == "offer" else "BUSINESS_INITIATED"
                            )
                            phone = contact_phone or (call.get("from") if direction == "USER_INITIATED" else call.get("to")) or existing.get("phone")
                            digits = "".join(character for character in str(phone or "") if character.isdigit())
                            conversation = (
                                await asyncio.to_thread(
                                    db[f"conversations_{org_id}"].find_one,
                                    {
                                        "business_phone_id": business_phone_id,
                                        "$or": [
                                            {"customer_id": {"$in": [phone, digits]}},
                                            {"conversation_id": {"$in": [f"whatsapp_{phone}", f"whatsapp_{digits}"]}},
                                        ],
                                    },
                                )
                                if phone else None
                            )
                            conversation_id = (conversation or {}).get("conversation_id") or existing.get("conversation_id")
                            now = datetime.now(timezone.utc)
                            provider_time = meta_datetime(call.get("timestamp"), now)
                            metadata_fields = {
                                "wa_id": whatsapp_business_id,
                                "business_phone_id": business_phone_id,
                                "business_phone": business_phone,
                                "direction": direction,
                                "conversation_id": conversation_id,
                                "contact_name": (conversation or {}).get("customer_name") or contact_name,
                                "phone": phone,
                            }
                            await asyncio.to_thread(
                                calls_collection.update_one,
                                {"call_id": call_id, "org_id": org_id},
                                {
                                    "$setOnInsert": {
                                        "call_id": call_id,
                                        "org_id": org_id,
                                        "created_at": provider_time,
                                        "started_at": meta_datetime(call.get("start_time"), provider_time),
                                    },
                                    "$set": metadata_fields,
                                },
                                upsert=True,
                            )
                            existing = await asyncio.to_thread(
                                calls_collection.find_one,
                                {"call_id": call_id, "org_id": org_id},
                            ) or metadata_fields
                            stored = await asyncio.to_thread(
                                _apply_call_lifecycle,
                                calls_collection,
                                org_id,
                                call_id,
                                existing,
                                raw_state=call.get("status") or call.get("event"),
                                direction=direction,
                                now=now,
                                provider_time=provider_time,
                                start_time=meta_datetime(call.get("start_time")),
                                end_time=meta_datetime(call.get("end_time")),
                                duration_seconds=call.get("duration"),
                                connected_event=False,
                            )
                            safe_call = dict(call)
                            sdp = session.get("sdp") if isinstance(session, dict) else None
                            if session is not None and (not isinstance(session, dict) or not isinstance(sdp, str) or len(sdp) > 100_000):
                                safe_call["session"] = {"sdp_type": session_type} if session_type else {}
                                logger.warning("Omitting invalid or oversized SDP from WhatsApp call {} websocket event", call_id)
                            call_payload = {
                                **safe_call,
                                "id": call_id,
                                "call_id": call_id,
                                "errors": safe_call.get("errors") or value.get("errors"),
                                "webhook_errors": value.get("errors"),
                                "direction": direction,
                                "conversation_id": conversation_id,
                                "contact_name": stored.get("contact_name"),
                                "phone": phone,
                                "status": stored.get("state"),
                                "agent_id": stored.get("agent_id"),
                                "agent_name": stored.get("agent_name"),
                                "agent_device_id": stored.get("agent_device_id"),
                                "handled_by": stored.get("handled_by"),
                                "created_at": stored.get("created_at"),
                                "started_at": stored.get("started_at"),
                                "connected_at": stored.get("connected_at"),
                                "ended_at": stored.get("ended_at"),
                                "duration_seconds": stored.get("duration_seconds"),
                            }
                            await broadcast_main_ws(whatsapp_business_id, "whatsapp", "whatsapp_call", call_payload)

                        for status in value.get("statuses") or []:
                            call_id = status.get("id")
                            if not call_id:
                                continue
                            existing = await asyncio.to_thread(
                                calls_collection.find_one,
                                {"call_id": call_id, "org_id": org_id},
                            ) or {}
                            now = datetime.now(timezone.utc)
                            status_value = status.get("status")
                            connected_event = str(status_value or "").upper() == "ACCEPTED"
                            provider_time = meta_datetime(status.get("timestamp"), now)
                            await asyncio.to_thread(
                                calls_collection.update_one,
                                {"call_id": call_id, "org_id": org_id},
                                {
                                    "$setOnInsert": {
                                        "call_id": call_id,
                                        "org_id": org_id,
                                        "created_at": provider_time,
                                        "started_at": provider_time,
                                    },
                                },
                                upsert=True,
                            )
                            existing = await asyncio.to_thread(
                                calls_collection.find_one,
                                {"call_id": call_id, "org_id": org_id},
                            ) or existing
                            stored = await asyncio.to_thread(
                                _apply_call_lifecycle,
                                calls_collection,
                                org_id,
                                call_id,
                                existing,
                                raw_state=status_value,
                                direction=existing.get("direction") or "USER_INITIATED",
                                now=now,
                                provider_time=provider_time,
                                connected_event=connected_event,
                            )
                            if connected_event and not stored.get("agent_id"):
                                await asyncio.to_thread(
                                    calls_collection.update_one,
                                    {
                                        "call_id": call_id,
                                        "org_id": org_id,
                                        "state": "connected",
                                        "$or": [{"agent_id": None}, {"agent_id": {"$exists": False}}],
                                    },
                                    {"$set": {"handled_by": "linked_device"}},
                                )
                                stored = await asyncio.to_thread(
                                    calls_collection.find_one,
                                    {"call_id": call_id, "org_id": org_id},
                                ) or stored
                            await broadcast_main_ws(whatsapp_business_id, "whatsapp", "whatsapp_call", {
                                **status,
                                "id": call_id,
                                "call_id": call_id,
                                "event": "status",
                                "status": stored.get("state") or status_value,
                                "direction": stored.get("direction"),
                                "conversation_id": stored.get("conversation_id"),
                                "contact_name": stored.get("contact_name"),
                                "phone": stored.get("phone"),
                                "agent_id": stored.get("agent_id"),
                                "agent_name": stored.get("agent_name"),
                                "agent_device_id": stored.get("agent_device_id"),
                                "handled_by": stored.get("handled_by"),
                                "created_at": stored.get("created_at"),
                                "started_at": stored.get("started_at"),
                                "connected_at": stored.get("connected_at"),
                                "ended_at": stored.get("ended_at"),
                                "duration_seconds": stored.get("duration_seconds"),
                            })
                        continue

                    # ============================================================================
                    # Handle status updates (message delivery/read receipts)
                    # ============================================================================
                    statuses = value.get("statuses", [])
                    for status in statuses:
                        message_id = status.get("id")
                        status_type = status.get("status")
                        
                        if not message_id or not status_type:
                            continue

                        delivery_error = (
                            normalize_whatsapp_delivery_error(status, stage="delivery")
                            if status_type == "failed" else None
                        )
                        logger.info(f"WhatsApp Status Update Received - Message ID: {message_id}, Status: {status_type}")

                        if status_type == "failed":
                            try:
                                shopify_error = await update_shopify_reminder_delivery(
                                    connection.get("org_id"), message_id, status_type, delivery_error,
                                )
                                if shopify_error:
                                    delivery_error = shopify_error
                            except Exception as exc:
                                logger.debug(
                                    "Shopify reminder delivery propagation skipped for {}: {}",
                                    message_id,
                                    type(exc).__name__,
                                )

                        # Update the message after Shopify augments retry state.
                        broadcast_message = await update_whatsapp_message_status(
                            org_id=connection.get("org_id"),
                            message_id=message_id,
                            status=status_type,
                            delivery_error=delivery_error,
                            status_payload=status,
                        )

                        delivery_task = (
                            connection.get("org_id"),
                            message_id,
                            status_type,
                            status,
                            delivery_error,
                            status_type != "failed",
                        )
                        if background_tasks:
                            background_tasks.add_task(_propagate_whatsapp_delivery_status, *delivery_task)
                        else:
                            asyncio.create_task(_propagate_whatsapp_delivery_status(*delivery_task))

                        if not broadcast_message:
                            logger.warning(f"No message found with ID {message_id} to update status.")
                            continue

                        if broadcast_message:
                            if "_id" in broadcast_message:
                                broadcast_message["_id"] = str(broadcast_message["_id"])

                            await broadcast_main_ws(
                                platform_id=whatsapp_business_id,
                                platform_type="whatsapp",
                                event_type="message_status_update",
                                payload={"message": broadcast_message}
                            )

                    # ============================================================================
                    
                    org_id = connection.get("org_id") 
                    access_token = connection.get("access_token")

                    # Get organization's automation flow collection
                    flow_collection_name = f"wa_bot_flows_{org_id}"
                    flow_collection = db[flow_collection_name]

                    # Get sender name from contacts if available
                    sender_name2 = "Unknown Sender"
                    contacts = value.get("contacts", [])
                    if contacts:
                        for contact in contacts:
                            sender_name2 = contact.get("profile", {}).get("name", "Unknown Sender")

                    # Process messages in the webhook
                    messages = value.get("messages", [])
                    if messages:
                        for message in messages:

                            # REACTION TYPE MESSAGES #######################################################################
                            if (message.get("reaction") is not None):

                                # message_timestamp = message.get("timestamp")
                                emoji = message.get("reaction").get("emoji")
                                message_id = message.get("reaction").get("message_id")
                                action = "react" if emoji else "unreact"
                                timestamp = datetime.now()

                                key = f"{message_id}_{emoji}_{action}"

                                # Check for duplicate messages reactions / if the message ID is already processed
                                if key in services.processed_message_reaction_ids:
                                    logger.info("Duplicate message, skipping processing.")
                                    continue
                                
                                # Add message ID with the current timestamp
                                services.processed_message_reaction_ids[key] = timestamp

                                # Extract essential data
                                customer_phone_no = normalize_phone(message.get("from"), is_canonical=True)
                                customer_name = sender_name2
                                message_type = message.get("type")

                                # Get user-specific collections
                                messages_collection_name = f"messages_{org_id}"
                                conversations_collection_name = f"conversations_{org_id}"
                                
                                # Get or create collections
                                messages_collection = db[messages_collection_name]
                                conversations_collection = db[conversations_collection_name]
                                
                                # Create conversation ID
                                conversation_id = whatsapp_conversation_id(customer_phone_no, business_phone_id)

                                logger.info(f"Reaction Event: {customer_name} {action} to message ID {message_id} {f'with emoji: {emoji}' if action == 'react' else ''} ")

                                if (action == "react"):
                                    # Update the message with the reaction info in  database and it for broadcasting
                                    try:
                                        # Update the message with the reaction info
                                        broadcast_message = await asyncio.to_thread(
                                            messages_collection.find_one_and_update,
                                            {"message_id": message_id},
                                            {"$set": {
                                                "reaction": {
                                                    "emoji": emoji,
                                                    "username": customer_phone_no,
                                                    "full_name": customer_name,
                                                    # "profile_picture_url": user_details.get("profile_pic"),
                                                    "timestamp": timestamp
                                                }
                                            }}, 
                                            return_document=ReturnDocument.AFTER,
                                        )
                                        
                                        if not broadcast_message:
                                            logger.warning(f"Message with ID {message_id} not found for reaction update")
                                            return {"status": "ok"}
                                            
                                    except Exception as e:
                                        logger.error(f"Error updating message with reaction: {e}")
                                        return {"status": "error", "message": str(e)}
                                    
                                elif (action == "unreact"):
                                    # Update the message with the reaction info in  database and it for broadcasting
                                    try:
                                        # Update the message with the reaction info
                                        broadcast_message = await asyncio.to_thread(
                                            messages_collection.find_one_and_update,
                                            {"message_id": message_id},
                                            {"$unset": {"reaction": 1}}, 
                                            return_document=ReturnDocument.AFTER,
                                        )
                                        
                                        if not broadcast_message:
                                            logger.warning(f"Message with ID {message_id} not found for reaction update")
                                            return {"status": "ok"}
                                            
                                    except Exception as e:
                                        logger.error(f"Error updating message with reaction: {e}")
                                        return {"status": "error", "message": str(e)}
                                


                                if "_id" in broadcast_message:
                                    broadcast_message["_id"] = str(broadcast_message["_id"])

                                try:
                                    from services.sales_learning_service import record_reaction
                                    await record_reaction(
                                        org_id, conversation_id,
                                        str(message.get("id") or f"wa:{message_id}:{message.get('timestamp')}:{action}:{emoji}"),
                                        str(message_id), str(emoji or ""), action, "whatsapp",
                                    )
                                except Exception as learning_error:
                                    logger.warning(f"Could not record internal WhatsApp reaction learning: {type(learning_error).__name__}")

                                await broadcast_main_ws(
                                    platform_id=whatsapp_business_id,
                                    platform_type="whatsapp",
                                    event_type="message_reaction",
                                    payload={"message": broadcast_message}
                                    )

                                # Broadcast the reaction update to connected clients
                                if whatsapp_business_id in services.whatsapp_connections_map:
                                    logger.success(f"Broadcasting message_reaction event to Frontend for {whatsapp_business_id}")
                                    await broadcast_whatsapp_message(whatsapp_business_id, "message_reaction", {
                                        "message": broadcast_message
                                    })

                                return {"status": "ok"}

                            # Skip processing if not a message
                            if "id" not in message:
                                continue
                                
                            # Check if message ID is already processed
                            message_id = message["id"]
                            wa_message_id = message_id  # Preserve wamid before it may be overwritten by store_whatsapp_message()
                            if message_id in services.processed_message_ids:
                                logger.info("Duplicate WhatsApp message, skipping processing.")
                                continue
                                
                            # Add message ID with the current timestamp
                            services.processed_message_ids[message_id] = time.time()
                            
                            # Extract essential data
                            customer_phone_no = normalize_phone(message.get("from"), is_canonical=True)
                            message_type = message.get("type")
                            content = normalize_whatsapp_message_content(message)
                            message_context = message.get("context")

                            # Get user-specific collections
                            messages_collection_name = f"messages_{org_id}"
                            conversations_collection_name = f"conversations_{org_id}"
                            
                            # Get or create collections
                            messages_collection = db[messages_collection_name]
                            conversations_collection = db[conversations_collection_name]
                            
                            # Create conversation ID
                            conversation_id = whatsapp_conversation_id(customer_phone_no, business_phone_id)

                            context_type = None
                            context = None
                            replied_message_id = None

                            try:
                                reply_window_info = calculate_whatsapp_reply_window(message)
                                reply_window_expiry_time_utc = reply_window_info["expiry_time_utc"]
                                is_ad_referral = reply_window_info["is_ad_referral"]
                            except ValueError as exc:
                                logger.warning(
                                    f"Failed to calculate WhatsApp reply window due to invalid/missing timestamp; "
                                    f"leaving the existing window unchanged. Error: {exc}"
                                )
                                reply_window_expiry_time_utc = None
                                is_ad_referral = False

                            if message_context:
                                replied_message_id = message_context.get("id")

                                if replied_message_id:
                                    logger.info(f"Message is a reply to message ID: {replied_message_id}")

                                    context_type = "message_reply"
                                    context = {
                                        "mid": replied_message_id
                                    }

                            stored_inbound_message = await asyncio.to_thread(
                                messages_collection.find_one,
                                {"message_id": wa_message_id},
                                {
                                    "conversation_id": 1,
                                    "ai_failure_category": 1,
                                    "ai_failure_status": 1,
                                    "ai_failure_pending": 1,
                                    "timestamp": 1,
                                },
                            )
                            pending_ai_failure = (
                                stored_inbound_message
                                if (stored_inbound_message or {}).get("ai_failure_pending")
                                else None
                            )
                            inbound_received_at = (
                                stored_inbound_message or {}
                            ).get("timestamp")
                            if not isinstance(inbound_received_at, datetime):
                                try:
                                    inbound_received_at = datetime.fromtimestamp(
                                        int(message.get("timestamp")), timezone.utc
                                    )
                                except (TypeError, ValueError, OverflowError):
                                    inbound_received_at = None
                            if pending_ai_failure:
                                conversation_id = pending_ai_failure.get("conversation_id") or conversation_id
                                await _handle_ai_provider_failure(
                                    AIProviderUnavailable(
                                        pending_ai_failure.get("ai_failure_category") or "unexpected_error",
                                        upstream_status=pending_ai_failure.get("ai_failure_status"),
                                    ),
                                    org_id=org_id,
                                    conversation_id=conversation_id,
                                    platform="whatsapp",
                                    customer_name=sender_name2,
                                    customer_number=customer_phone_no,
                                    source_message_id=wa_message_id,
                                    state_already_checked=True,
                                    resume_pending=True,
                                )
                                return {"status": "ok"}
                                    
                            # Get conversation doc to check if AI is enabled
                            convo_doc = await asyncio.to_thread(
                                conversations_collection.find_one,
                                {"conversation_id": conversation_id},
                            )
                            is_conv_ai_enabled = convo_doc.get("is_ai_enabled", False) if convo_doc else True
                            stage = convo_doc.get("onboarding_stage", "welcome") if convo_doc else "welcome"
                            
                            org_doc = await asyncio.to_thread(
                                db.organizations.find_one,
                                {"org_id": org_id},
                                {"is_ai_enabled": 1},
                            )
                            is_org_ai_enabled = org_doc.get("is_ai_enabled", False) if org_doc else False
                            
                            is_heidelai = whatsapp_business_id == HEIDELAI_ORG_WA_ID
                            is_pma_wa = whatsapp_business_id == PARIJAT_ACADEMY_ORG_WA_ID
                            is_lee_dealer_wa = whatsapp_business_id == LEE_DEALER_ORG_WA_ID
                            is_karan_wa = whatsapp_business_id == KARAN_ORG_WA_ID
                            is_giga_wa = whatsapp_business_id == GIGA_ORG_WA_ID

                            is_komal_wa = whatsapp_business_id == KOMAL_ORG_WA_ID
                            is_production = ENVIRONMENT == "production"
                            is_dev = ENVIRONMENT == "development"
                            is_test = ENVIRONMENT == "test"
                            # fetch org + convo flags
                            ai_enabled = is_org_ai_enabled and is_conv_ai_enabled

                            logger.info(f"is_heidelai: {is_heidelai}")
                            logger.info(f"is_pma_wa: {is_pma_wa}")
                            logger.info(f"is_lee_dealer_wa: {is_lee_dealer_wa}")
                            logger.info(f"is_karan_wa: {is_karan_wa}")
                            logger.info(f"is_giga_wa: {is_giga_wa}")
                            logger.info(f"is_komal_wa: {is_komal_wa}")
                            logger.info(f"is_production: {is_production}")
                            logger.info(f"is_dev: {is_dev}")
                            logger.info(f"is_test: {is_test}")
                            logger.info(f"ai_enabled: {ai_enabled}") 

                            bound_flow_id = connection.get("wa_bot_flow_id")
                            whatsapp_flow = await asyncio.to_thread(
                                flow_collection.find_one,
                                {
                                    "flow_id": bound_flow_id,
                                    "trigger_from": "webhook",
                                    "trigger_event": "message_received",
                                    "status": "active",
                                },
                            ) if bound_flow_id else None
                            logger.info(
                                f"[{org_id}] WhatsApp automation flow: "
                                f"{'found' if whatsapp_flow else 'no active flow found'}"
                            )

                            # Extract message content based on message type
                            if message_type == "text":
                                try:
                                    await asyncio.to_thread(record_whatsapp_preference, org_id, customer_phone_no, content)
                                except Exception:
                                    services.processed_message_ids.pop(wa_message_id, None)
                                    raise

                                # Dealer membership is a live capability, not a
                                # permanent conversation role. Revalidate it so
                                # deactivated directory entries cannot leave a
                                # conversation stuck in the dealer path forever.
                                was_dealer = bool(convo_doc and convo_doc.get("is_dealer"))
                                dealer_lookup_ok = True
                                try:
                                    is_dealer = bool(await asyncio.to_thread(
                                        db.dealer_directory.find_one,
                                        {
                                            "org_id": org_id,
                                            "phone": customer_phone_no,
                                            "active": True,
                                        },
                                    ))
                                except Exception:
                                    logger.warning(
                                        f"Could not refresh dealer state for ...{customer_phone_no[-4:]}; "
                                        "keeping the existing conversation state",
                                        exc_info=True,
                                    )
                                    is_dealer = was_dealer
                                    dealer_lookup_ok = False
                                dealer_state_updates = {}
                                current_persona = str(
                                    (convo_doc or {}).get("persona") or "fallback"
                                )
                                is_dealer_persona = (
                                    current_persona.strip().lower().rstrip("s") == "dealer"
                                )
                                if dealer_lookup_ok:
                                    if is_dealer != was_dealer:
                                        dealer_state_updates["is_dealer"] = is_dealer
                                    if is_dealer and not is_dealer_persona:
                                        dealer_state_updates["persona"] = "dealer"
                                    elif not is_dealer and is_dealer_persona:
                                        dealer_state_updates["persona"] = "fallback"

                                if dealer_state_updates:
                                    logger.info(
                                        f"Refreshing dealer state for ...{customer_phone_no[-4:]} "
                                        f"in org {org_id}: active={is_dealer}"
                                    )
                                    if convo_doc:
                                        convo_doc.update(dealer_state_updates)
                                    else:
                                        convo_doc = {
                                            "conversation_id": conversation_id,
                                            "is_ai_enabled": True,
                                            **dealer_state_updates,
                                        }

                                # Check if this is a dealer reply to an out-of-stock request
                                is_dealer_reply = await check_and_handle_dealer_reply(
                                    org_id=org_id,
                                    customer_phone_no=customer_phone_no,
                                    content=content,
                                    message_context=message_context,
                                    create_escalation=True,
                                    source_message_id=wa_message_id,
                                )
                                # Storing messages from customers in the DB
                                # Create or update conversation
                                conversation_id = await create_or_update_whatsapp_conversation(
                                        org_id=org_id,
                                        recipient_id=whatsapp_business_id,
                                        customer_phone_no=customer_phone_no,
                                        customer_name=sender_name2,
                                        last_message=content,
                                        last_sender="customer",
                                        reply_window_expiry=reply_window_expiry_time_utc,
                                        is_ad_referral=is_ad_referral,
                                        mode="reply",
                                        extra_updates=dealer_state_updates or None,
                                        idempotency_key=wa_message_id,
                                        business_phone_id=business_phone_id,
                                        business_phone=business_phone,
                                    )
                                    
                                # Store in WhatsApp-specific collections
                                message_id = await store_whatsapp_message(
                                        org_id=org_id,
                                        content=content,
                                        conversation_id=conversation_id,
                                        customer_phone_no=customer_phone_no,
                                        type=message_type,
                                        payload=message.get("payload"),
                                        sender_name=sender_name2,
                                        recipient_id=whatsapp_business_id,
                                        role=MessageRole.CUSTOMER,
                                        message_id=message_id,  # Use the ID from the webhook for deduplication
                                        raw_data=message,
                                        mode="reply",
                                        context_type=context_type,
                                        context=context,
                                        notify_push=True,
                                        business_phone_id=business_phone_id,
                                    )

                                # === Purchase Intent Background Check ===
                                # Queue this message for background AI analysis.
                                # Non-blocking, non-critical — failure here MUST NOT affect the webhook.
                                try:
                                    await asyncio.to_thread(
                                        enqueue_intent_check,
                                        org_id=org_id,
                                        conversation_id=conversation_id,
                                        customer_phone=customer_phone_no,
                                        customer_name=sender_name2,
                                        message_content=content,
                                    )
                                except Exception:
                                    pass  # silently ignore — intent extraction is best-effort

                                if is_dealer_reply:
                                    logger.info("Dealer reply stored and intercepted. Skipping buyer pricing and AI flow.")
                                    return {"status": "ok"}

                                if await handle_followup_customer_reply(org_id, conversation_id, content):
                                    logger.info(f"Follow-up reply intercepted for {conversation_id}")
                                    return {"status": "ok"}

                                try:
                                    pricing_result = await observe_customer_message(
                                        org_id=org_id,
                                        conversation_id=conversation_id,
                                        content=content,
                                        source_message_id=wa_message_id,
                                        reply_to_message_id=replied_message_id,
                                        customer_name=sender_name2,
                                    )
                                except Exception as exc:
                                    logger.error(f"Pricing guard failed for org={org_id}: {exc}")
                                    review_mode = await pricing_runtime_mode(org_id) == "review"
                                    pricing_result = pricing_failure_intercept(
                                        review_mode and is_price_signal(content)
                                    )
                                    if pricing_result["intercept"]:
                                        await _handle_ai_provider_failure(
                                            AIProviderUnavailable("catalogue_unavailable"),
                                            org_id=org_id,
                                            conversation_id=conversation_id,
                                            platform="whatsapp",
                                            customer_name=sender_name2,
                                            customer_number=customer_phone_no,
                                            source_message_id=wa_message_id,
                                        )

                                if await send_pricing_intercept_reply(
                                    pricing_result,
                                    org_id=org_id,
                                    conversation_id=conversation_id,
                                    whatsapp_business_id=whatsapp_business_id,
                                    customer_phone_no=customer_phone_no,
                                    customer_name=sender_name2,
                                    source_message_id=wa_message_id,
                                ):
                                    return {"status": "ok"}

                                contact_data = {
                                    "whatsapp_id": whatsapp_business_id,
                                    "full_name": sender_name2,
                                    "phone_number": customer_phone_no,
                                    "country": None,
                                    "profile_url": None,
                                    "conversation_id": conversation_id,
                                    "email": None,
                                    "categories": []
                                }
                                store_contact_background(background_tasks, org_id, "whatsapp", contact_data)



                                #  Don't execute for heidel_ai as it is not on whatsapp flow yet but has AI

                                    # Execute the WhatsApp Automation Flow first (if it exists)
                                    # Runs on all environments and regardless of AI toggle
                                if whatsapp_flow:
                                    automation_response = await execute_whatsapp_flow(
                                        flow=whatsapp_flow,
                                        org_id=org_id,
                                        conversation_id=conversation_id,
                                        stage=stage,
                                        trigger_event="message_received",
                                        trigger_messages=whatsapp_flow.get("trigger_messages"),
                                        message_content=content,
                                        customer_name=sender_name2,
                                        whatsapp_business_id=whatsapp_business_id,
                                        message_type=message_type,
                                        source_message_id=wa_message_id,
                                    )
                                    # If the flow handled the message successfully, exit early.
                                    if automation_response:
                                        logger.info(f"[AI_ROUTING] [{conversation_id}] Message handled by WhatsApp automation flow. Skipping AI.")
                                        await read_message_and_typing_indicator(whatsapp_id=whatsapp_business_id, message_id=message_id, conversation_id=conversation_id)
                                        return {"status": "ok"}
                                # Fallback to AI Agent (only if AI is enabled for this org+conversation)
                                if ai_enabled and await is_effective_ai_enabled(org_id, conversation_id):
                                    if not convo_doc:
                                        convo_doc = await asyncio.to_thread(
                                            conversations_collection.find_one,
                                            {"conversation_id": conversation_id},
                                        )
                                    # Fallback to Agent (executes if no flow exists OR flow didn't return a response)
                                    logger.info(f"[AI_ROUTING] [{conversation_id}] ✅ Sending message to AI agent. org={org_id}, is_heidelai={is_heidelai}, content_length={len(content)}")
                                    try:
                                        product_reply_started = time.perf_counter()
                                        asyncio.create_task(
                                            _best_effort_typing(
                                                whatsapp_business_id,
                                                message_id,
                                                conversation_id,
                                            )
                                        )
                                        chatbot_response = await send_to_agent(
                                            conversation_id=conversation_id,
                                            user_input=content,
                                            persona=convo_doc.get("persona", "fallback"),
                                            location=convo_doc.get("location", "india"),
                                            customer_name=sender_name2,
                                            org_id=org_id,
                                            source="webhook",
                                            reply_context_message_id=replied_message_id,
                                            message_id=wa_message_id,
                                            message_type=message_type,
                                        )
                                        if not await is_effective_ai_enabled(org_id, conversation_id):
                                            return {"status": "ok"}

                                        sales_action = chatbot_response.get("sales_action")
                                        agent_reply_to = chatbot_response.get("reply_to_message_id")
                                        meta_send_started_wall = datetime.now(timezone.utc)
                                        meta_send_started = time.perf_counter()
                                        send_result = await send_whatsapp_message({
                                            "conversation_id": conversation_id,
                                            "content": chatbot_response["final_response"],
                                            "sender_id": whatsapp_business_id,
                                            "pricing_source_message_id": None if sales_action else wa_message_id,
                                            "ai_usage": chatbot_response.get("usage"),
                                        }, org_id, "ai", temp_id=f"ai:{wa_message_id}",
                                            context_type="message_reply" if agent_reply_to else None,
                                            context={"mid": agent_reply_to} if agent_reply_to else None,
                                            bot_context={"sales_action_job_id": sales_action.get("job_id")} if sales_action else None,
                                        )
                                        _log_product_reply_latency(
                                            org_id, conversation_id, chatbot_response, send_result,
                                            product_reply_started, meta_send_started,
                                            inbound_received_at, meta_send_started_wall,
                                        )
                                        await _finalize_whatsapp_sales_action(
                                            org_id=org_id,
                                            conversation_id=conversation_id,
                                            action=sales_action,
                                            send_result=send_result,
                                            customer_name=sender_name2,
                                            customer_number=customer_phone_no,
                                            source_message_id=wa_message_id,
                                        )
                                    except AIProviderUnavailable as exc:
                                        await _handle_ai_provider_failure(
                                            exc,
                                            org_id=org_id,
                                            conversation_id=conversation_id,
                                            platform="whatsapp",
                                            customer_name=sender_name2,
                                            customer_number=customer_phone_no,
                                            source_message_id=wa_message_id,
                                        )
                                        return {"status": "ok"}
                                else:
                                    await _send_quota_handoff(org_id=org_id, conversation_id=conversation_id, platform="whatsapp", customer_number=customer_phone_no, customer_name=sender_name2)
                                # Check and send OOS dealer messages if a recent request was logged
                                try:
                                    from services.oos_service import send_oos_to_dealers
                                    recent_oos = await asyncio.to_thread(
                                        db.oos_requests.find_one,
                                        {"org_id": org_id, "conversation_id": conversation_id, "status": "pending_send"},
                                        sort=[("created_at", -1)],
                                    )
                                    if recent_oos:
                                        created_at = recent_oos.get("created_at")
                                        if created_at:
                                            if created_at.tzinfo is None:
                                                created_at = created_at.replace(tzinfo=timezone.utc)
                                            time_diff = (datetime.now(timezone.utc) - created_at).total_seconds()
                                            if time_diff < 30:
                                                await send_oos_to_dealers(recent_oos, org_id)
                                except Exception as e:
                                    logger.error(f"Error checking/sending OOS dealer message: {e}")
                                return {"status": "ok"}

                                # Handling messages received on HeidelAI's whatsapp number
                                # if is_heidelai and ai_enabled and is_production:

                                #     chatbot_response = await send_to_chatbot(
                                #         customer_phone_no=customer_phone_no,
                                #         message_content=content,
                                #         message_id=wa_message_id
                                #     )
                                
                                #     if not chatbot_response:
                                #         return {"status": "ok"}

                                #     # Extract data preserving backward compatibility
                                #     if isinstance(chatbot_response, dict):
                                #         response_text = chatbot_response.get("response", "")
                                #         reply_to_message_id = chatbot_response.get("reply_to_message_id")
                                #     else:
                                #         response_text = chatbot_response
                                #         reply_to_message_id = None

                                #     # Storing chatbot response in the DB
                                #     # Create or update conversation
                                #     conversation_id = await create_or_update_whatsapp_conversation(
                                #         org_id=org_id,
                                #         recipient_id=whatsapp_business_id,
                                #         customer_phone_no=customer_phone_no,
                                #         customer_name=sender_name2,
                                #         last_message=response_text,
                                #         last_sender="ai",
                                #         mode="reply"
                                #     )
                                    
                                #     msg_kwargs = {
                                #         "message": {
                                #             "conversation_id": conversation_id,
                                #             "content": response_text,
                                #             "sender_id": HEIDELAI_ORG_WA_ID,
                                #         },
                                #         "org_id": org_id,
                                #         "mode": "ai"
                                #     }
                                    
                                #     if reply_to_message_id:
                                #         msg_kwargs["context_type"] = "message_reply"
                                #         msg_kwargs["context"] = {"mid": reply_to_message_id}

                                #     # Storing in WhatsApp messages collection is handled by send_whatsapp_message()
                                #     res = await send_whatsapp_message(**msg_kwargs)

                                #     return {"status": "ok"}

                            elif is_pma_wa and ai_enabled and is_production:

                                flow_response = await process_onboarding_if_needed(user_input=content, conversation_id=conversation_id, org_id=org_id, stage=stage, whatsapp_business_id=whatsapp_business_id, sender_name=sender_name2)
                                        
                                if flow_response:
                                    await send_interactive_whatsapp_message(
                                            org_id=org_id,
                                            conversation_id=conversation_id,
                                            sender_id=whatsapp_business_id,
                                            interactive_message=flow_response
                                        )

                                    return {"status": "ok"}

                                if not await is_effective_ai_enabled(org_id, conversation_id):
                                    await _send_quota_handoff(org_id=org_id, conversation_id=conversation_id, platform="whatsapp", customer_number=customer_phone_no, customer_name=sender_name2)
                                    return {"status": "ok"}
                                try:
                                    chatbot_response = await send_to_pma_chatbot(
                                                conversation_id=conversation_id,
                                                user_input=content,
                                                persona=convo_doc.get("persona", "fallback"),
                                                location=convo_doc.get("location", "india"),
                                                customer_name=sender_name2,
                                                org_id=org_id,
                                                source="webhook",
                                                reply_context_message_id=replied_message_id,
                                                message_id=wa_message_id,
                                        )
                                    if not await is_effective_ai_enabled(org_id, conversation_id):
                                        return {"status": "ok"}
                                    await send_whatsapp_message({
                                        "conversation_id": conversation_id,
                                        "content": chatbot_response["final_response"],
                                        "sender_id": PARIJAT_ACADEMY_ORG_WA_ID,
                                        "ai_usage": chatbot_response.get("usage"),
                                        }, org_id, "ai", temp_id=f"ai:{wa_message_id}")
                                except AIProviderUnavailable as exc:
                                    await _handle_ai_provider_failure(
                                        exc,
                                        org_id=org_id,
                                        conversation_id=conversation_id,
                                        platform="whatsapp",
                                        customer_name=sender_name2,
                                        customer_number=customer_phone_no,
                                        source_message_id=wa_message_id,
                                    )
                                return {"status": "ok"}

                            elif ai_enabled and is_production and is_lee_dealer_wa:
                                # Fallback to Agent (executes if no flow exists OR flow didn't return a response)

                                if not await is_effective_ai_enabled(org_id, conversation_id):
                                    await _send_quota_handoff(org_id=org_id, conversation_id=conversation_id, platform="whatsapp", customer_number=customer_phone_no, customer_name=sender_name2)
                                    return {"status": "ok"}
                                try:
                                    product_reply_started = time.perf_counter()
                                    chatbot_response = await send_to_agent(
                                        conversation_id=conversation_id,
                                        user_input=content,
                                        persona="dealers",
                                        location=convo_doc.get("location", "india"),
                                        customer_name=sender_name2,
                                        org_id=org_id,
                                        source="webhook",
                                        reply_context_message_id=replied_message_id,
                                        message_id=wa_message_id,
                                        message_type=message_type,
                                    )
                                    if not await is_effective_ai_enabled(org_id, conversation_id):
                                        return {"status": "ok"}
                                    sales_action = chatbot_response.get("sales_action")
                                    agent_reply_to = chatbot_response.get("reply_to_message_id")
                                    meta_send_started_wall = datetime.now(timezone.utc)
                                    meta_send_started = time.perf_counter()
                                    send_result = await send_whatsapp_message({
                                        "conversation_id": conversation_id,
                                        "content": chatbot_response["final_response"],
                                        "sender_id": whatsapp_business_id,
                                        "pricing_source_message_id": None if sales_action else wa_message_id,
                                        "ai_usage": chatbot_response.get("usage"),
                                    }, org_id, "ai", temp_id=f"ai:{wa_message_id}",
                                        context_type="message_reply" if agent_reply_to else None,
                                        context={"mid": agent_reply_to} if agent_reply_to else None,
                                        bot_context={"sales_action_job_id": sales_action.get("job_id")} if sales_action else None,
                                    )
                                    _log_product_reply_latency(
                                        org_id, conversation_id, chatbot_response, send_result,
                                        product_reply_started, meta_send_started,
                                        inbound_received_at, meta_send_started_wall,
                                    )
                                    await _finalize_whatsapp_sales_action(
                                        org_id=org_id,
                                        conversation_id=conversation_id,
                                        action=sales_action,
                                        send_result=send_result,
                                        customer_name=sender_name2,
                                        customer_number=customer_phone_no,
                                        source_message_id=wa_message_id,
                                    )
                                except AIProviderUnavailable as exc:
                                    await _handle_ai_provider_failure(
                                        exc,
                                        org_id=org_id,
                                        conversation_id=conversation_id,
                                        platform="whatsapp",
                                        customer_name=sender_name2,
                                        customer_number=customer_phone_no,
                                        source_message_id=wa_message_id,
                                    )

                                return {"status": "ok"}

                            elif message_type in ["image", "video", "document", "audio"]:
                                media_data = message[message_type]
                                media_url = media_data.get("url")
                                media_id = media_data["id"]
                                mime_type = media_data["mime_type"]
                                media_name = media_data.get("filename", f"{message_type}_{media_id}")

                                # Make the new conversation and notification durable before the
                                # external media upload; Cloudinary may be slow or unavailable.
                                try:
                                    conversation_id = await create_or_update_whatsapp_conversation(
                                        org_id=org_id,
                                        recipient_id=whatsapp_business_id,
                                        customer_phone_no=customer_phone_no,
                                        customer_name=sender_name2,
                                        last_message=content,
                                        last_sender="customer",
                                        reply_window_expiry=reply_window_expiry_time_utc,
                                        is_ad_referral=is_ad_referral,
                                        mode="reply",
                                        business_phone_id=business_phone_id,
                                        business_phone=business_phone,
                                    )
                                    await store_whatsapp_message(
                                        org_id=org_id,
                                        content=content,
                                        conversation_id=conversation_id,
                                        customer_phone_no=customer_phone_no,
                                        type=message_type,
                                        payload={
                                            "media_id": media_id,
                                            "mime_type": mime_type,
                                            "file_name": media_name,
                                            "media_status": "processing",
                                        },
                                        recipient_id=whatsapp_business_id,
                                        role=MessageRole.CUSTOMER,
                                        sender_name=sender_name2,
                                        message_id=message_id,
                                        status="received",
                                        context_type=context_type,
                                        context=context,
                                        notify_push=True,
                                        business_phone_id=business_phone_id,
                                    )
                                except Exception:
                                    services.processed_message_ids.pop(message_id, None)
                                    raise

                                background_tasks.add_task(
                                    upload_and_send_whatsapp_media_background,
                                    media_url=media_url,
                                    media_id=media_id,
                                    org_id=org_id,
                                    mime_type=mime_type,
                                    access_token=access_token,
                                    message_type=message_type,
                                    whatsapp_business_id=whatsapp_business_id,
                                    message_id=message_id,
                                    media_name=media_name,
                                    caption=media_data.get("caption", None),
                                    phone_number_id=value.get("metadata", {}).get("phone_number_id") or connection.get("phone_id"),
                                )

                                if await handle_followup_customer_reply(org_id, conversation_id, content):
                                    logger.info(f"Media reply handled for follow-up {conversation_id}")
                                    return {"status": "ok"}

                                caption = media_data.get("caption")
                                if caption and is_price_signal(caption):
                                    try:
                                        pricing_result = await observe_customer_message(
                                            org_id=org_id,
                                            conversation_id=conversation_id,
                                            content=caption,
                                            source_message_id=wa_message_id,
                                            reply_to_message_id=replied_message_id,
                                            customer_name=sender_name2,
                                        )
                                        if await send_pricing_intercept_reply(
                                            pricing_result,
                                            org_id=org_id,
                                            conversation_id=conversation_id,
                                            whatsapp_business_id=whatsapp_business_id,
                                            customer_phone_no=customer_phone_no,
                                            customer_name=sender_name2,
                                            source_message_id=wa_message_id,
                                        ):
                                            return {"status": "ok"}
                                    except Exception as exc:
                                        logger.error(f"Media-caption pricing observation failed: {exc}")
                                        if await pricing_runtime_mode(org_id) == "review":
                                            await _handle_ai_provider_failure(
                                                AIProviderUnavailable("catalogue_unavailable"),
                                                org_id=org_id,
                                                conversation_id=conversation_id,
                                                platform="whatsapp",
                                                customer_name=sender_name2,
                                                customer_number=customer_phone_no,
                                                source_message_id=wa_message_id,
                                            )
                                            await send_pricing_intercept_reply(
                                                pricing_failure_intercept(True),
                                                org_id=org_id,
                                                conversation_id=conversation_id,
                                                whatsapp_business_id=whatsapp_business_id,
                                                customer_phone_no=customer_phone_no,
                                                customer_name=sender_name2,
                                                source_message_id=wa_message_id,
                                            )
                                            return {"status": "ok"}

                                contact_data = {
                                "whatsapp_id": whatsapp_business_id,
                                "full_name": sender_name2,
                                "phone_number": customer_phone_no,
                                "country": None,
                                "profile_url": None,
                                "conversation_id": conversation_id,
                                "email": None,
                                "categories": []
                                }

                                store_contact_background(background_tasks, org_id, "whatsapp", contact_data)

                                return {"status": "ok"}
                            elif message_type == "location":
                                content = "Shared a location"
                            elif message_type == "button":
                                content = f"[Button: {message.get('button', {}).get('payload', 'Unknown')}]"
                            elif message_type == "order":
                                content = "Shared an order"
                            elif message_type == "interactive":
                                interactive_data = message.get("interactive", {})
                                
                                # Determine if it's a list or button reply
                                interaction_type = interactive_data.get("type")
                    
                                # Larger scope variables to access later below in the block
                                selection_id = None
                                selection_title = None
                    
                                # Extract the specific payload/ID and title into larger scope variables
                                if interaction_type == "list_reply":
                                    reply = interactive_data.get("list_reply", {})
                                    logger.info("Interaction type is list")

                                    selection_id = reply.get("id")      # Predefined Row ID
                                    selection_title = reply.get("title")
                                        
                                elif interaction_type == "button_reply":
                                    reply = interactive_data.get("button_reply", {})
                                    logger.info("Interaction type is button")

                                    selection_id = reply.get("id")      # Predefined Button ID
                                    selection_title = reply.get("title")
                
                                content = selection_title
                                logger.info(f"Extracted content from interactive message: {content}, selection_id: {selection_id}")
                                # Storing messages from customers in the DB
                                # Create or update conversation
                                conversation_id = await create_or_update_whatsapp_conversation(
                                        org_id=org_id,
                                        recipient_id=whatsapp_business_id,
                                        customer_phone_no=customer_phone_no,
                                        customer_name=sender_name2,
                                        last_message=content,
                                        last_sender="customer",
                                        reply_window_expiry=reply_window_expiry_time_utc,
                                        is_ad_referral=is_ad_referral,
                                        mode="reply",
                                        business_phone_id=business_phone_id,
                                        business_phone=business_phone,
                                    )
                                    
                                # Store in WhatsApp-specific collections
                                message_id = await store_whatsapp_message(
                                        org_id=org_id,
                                        content=content,
                                        conversation_id=conversation_id,
                                        customer_phone_no=customer_phone_no,
                                        type=message_type,
                                        payload=interactive_data,
                                        sender_name=sender_name2,
                                        recipient_id=whatsapp_business_id,
                                        role=MessageRole.CUSTOMER,
                                        message_id=message_id,  # Use the ID from the webhook for deduplication
                                        raw_data=message,
                                        mode="reply",
                                        context_type=context_type,
                                        context=context,
                                        notify_push=True,
                                        business_phone_id=business_phone_id,
                                    )

                                if await handle_followup_customer_reply(org_id, conversation_id, content or "Interactive reply"):
                                    logger.info(f"Follow-up reply intercepted for {conversation_id}")
                                    return {"status": "ok"}

                                if whatsapp_flow:
                                    automation_response = await execute_whatsapp_flow(
                                        flow=whatsapp_flow,
                                        org_id=org_id,
                                        conversation_id=conversation_id,
                                        stage=stage,
                                        trigger_event="message_received",
                                        trigger_messages=whatsapp_flow.get("trigger_messages") if isinstance(whatsapp_flow, dict) else None,
                                        message_content=selection_id if selection_id else None,
                                        customer_name=sender_name2,
                                        whatsapp_business_id=whatsapp_business_id,
                                        message_type=message_type,
                                        source_message_id=wa_message_id,
                                    )

                                    if automation_response:
                                        return {"status": "ok"}
                                
                                if is_pma_wa and ai_enabled and is_production:
                                    if selection_id:
                                        match selection_id:
                                            case "india" | "abroad":
                                                flow_response = await process_onboarding_if_needed(user_input=selection_id, conversation_id=conversation_id, org_id=org_id, stage="awaiting_location", whatsapp_business_id=whatsapp_business_id, sender_name=sender_name2)
                                                if flow_response:
                                                    await send_interactive_whatsapp_message(
                                                        org_id=org_id,
                                                        conversation_id=conversation_id,
                                                        sender_id=whatsapp_business_id,
                                                        interactive_message=flow_response
                                                    )
                                                    return {"status": "ok"}

                                            case "free_master_class":
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

                                                return {"status": "ok"}

                                            case "abacus_students_5_to_9":
                                                # Send info about the Abacus course
                                                await send_whatsapp_message(
                                                    org_id=org_id,
                                                    mode="reply",
                                                    message={
                                                        "sender_id" : whatsapp_business_id,
                                                        "conversation_id" : conversation_id,
                                                        "content": (
                                                        "🔢 *Abacus Program*\n\n"
                                                        
                                                        "Age: *5-9 years*\n\n"
                                                        
                                                        "Course Structure:\n"
                                                        "- *8 levels*\n"
                                                        "- Classes: *2 days per week*"

                                                        "\n\n*Benefits*\n"
                                                        "✅ Improves memory\n"
                                                        "✅ Strong number sense\n"
                                                        "✅ Faster mental calculations\n"
                                                        "✅ Better concentration\n\n"
                                                    )
                                                    }
                                                )

                                                return {"status": "ok"}
                                               
                                            case "vedic_maths_regular_9_plus":
                                                # Send info about Vedic Maths for Students
                                                await send_interactive_whatsapp_message(
                                                    org_id=org_id,
                                                    conversation_id=conversation_id,
                                                    sender_id=whatsapp_business_id,
                                                    interactive_message=InteractiveReplyButtonMessage(
                                                        type="button",
                                                        header={
                                                            "type": "text",
                                                            "text": "🧠 Vedic Maths Program"
                                                        },
                                                        body_text=(
                                                            "Age: *9 years and above*\n\n"
                                                            
                                                            "Course Structure:\n"
                                                            "- *Five Structured Volumes*\n"
                                                            "- Classes: *2 days per week*"

                                                            "\n\n*Benefits*\n"
                                                            "✅ Faster calculations\n"
                                                            "✅ Improves concentration\n"
                                                            "✅ Boosts confidence in Maths\n"
                                                            "✅ Helpful for school & competitive exams\n\n"
                                                        ),
                                                        buttons=[
                                                            ActionButton(
                                                                type="reply",
                                                                reply=ReplyButton(
                                                                    id="contact_details_for_vedic_maths_regular", 
                                                                    title="Contact Details"
                                                                )
                                                            ),
                                                            ActionButton(
                                                                type="reply",
                                                                reply=ReplyButton(
                                                                    id="fees_structure_for_vedic_maths_regular", 
                                                                    title="Fees Structure"
                                                                )
                                                            ),
                                                        ],
                                                    )
                                                )

                                                return {"status": "ok"}                                

                                            case "vedic_maths_books":
                                                await send_interactive_whatsapp_message(
                                                    org_id=org_id,
                                                    conversation_id=conversation_id,
                                                    sender_id=whatsapp_business_id,
                                                    interactive_message=get_vedic_maths_books_message()
                                                )
                                                
                                                await send_whatsapp_message(
                                                    org_id=org_id,
                                                    mode="reply",
                                                    message={
                                                        "sender_id": whatsapp_business_id,
                                                        "conversation_id": conversation_id,
                                                        "content": (
                                                            "https://youtube.com/watch?v=ztvJiASRm0w&si=t2tiBQN9ZqO92Ua3"
                                                        ),
                                                        "preview_url": True,
                                                    }
                                                )

                                                return {"status": "ok"}

                                            case "vedic_maths_teacher":
                                                # Send info about Teacher Training
                                                await send_whatsapp_message(
                                                    org_id=org_id,
                                                    mode="reply",
                                                    message={
                                                        "sender_id" : whatsapp_business_id,
                                                        "conversation_id" : conversation_id,
                                                        "content": (
                                                        "🔢 *Vedic Maths Teacher Certification*\n\n"
                                                        
                                                        "Duration: *40 hours*\n"
                                                        "Schedule: *3 days per week*\n\n"

                                                        "*Benefits*\n"
                                                        "✅ Become a Certified Vedic Maths Trainer\n"
                                                        "✅ Start your own classes\n"
                                                        "✅ Teach online or offline\n"
                                                        "✅ Earn additional income\n\n"
                                                    )
                                                    }
                                                )

                                                return {"status": "ok"}

                                            case "contact_details_for_vedic_maths_regular":
                                                await send_interactive_whatsapp_message(
                                                    org_id=org_id,
                                                    conversation_id=conversation_id,
                                                    sender_id=whatsapp_business_id,
                                                    interactive_message=InteractiveUrlButtonMessage(
                                                        type="cta_url",
                                                        body_text=(
                                                            "Thanks for showing interest in our *Vedic Maths Program*\n\n"

                                                            "Please click on the below button for contacting us."
                                                        ),
                                                        url="https://paarijatacademy.in/courses/",
                                                        button_text="Details & Contact"
                                                    )
                                                )

                                                return {"status": "ok"}
                                        
                                        
                                            case "order_books":
                                                await send_whatsapp_message(
                                                    org_id=org_id,
                                                    mode="reply",
                                                    message={
                                                        "sender_id" : whatsapp_business_id,
                                                        "conversation_id" : conversation_id,
                                                        "content": (
                                                        "*Please contact below numbers to place order*\n\n"
                                                        
                                                        "📱 9503498556\n"
                                                        "📱 8055579965\n\n"
                                                    )
                                                    }
                                                )

                                                return {"status": "ok"}

                                            case "fees_structure_for_vedic_maths_regular":
                                                await send_whatsapp_message(
                                                    org_id=org_id,
                                                    mode="reply",
                                                    message={
                                                        "sender_id" : whatsapp_business_id,
                                                        "conversation_id" : conversation_id,
                                                        "content": (
                                                            "https://connectitapp.in/parijat-academy\n\n"


                                                            "Please find the QR inside and pay Rs 7500/-\n\n"

                                                            "Registration Fees : Rs 500\n"
                                                            "Level 1 : 3500 \n"
                                                            "Level 2 : 3500\n"
                                                            "2 Books : 400\n"
                                                            "Total : 7900/-\n\n"

                                                            "Payment in One Installment and get Rs 500 discount.\n\n"

                                                            "Payment Only \n"
                                                            "7000 + 400 = 7400/-\n\n"

                                                            "Please send Receipt after payment"
                                                        )
                                                    }
                                                )

                                                return {"status": "ok"}

                                            # --- Fallback for unknown IDs ---
                                            case _:
                                                if not await is_effective_ai_enabled(org_id, conversation_id):
                                                    return {"status": "ok"}
                                                try:
                                                    chatbot_response = await send_to_pma_chatbot(
                                                        conversation_id=conversation_id,
                                                        user_input=content,
                                                        persona="fallback",
                                                        location="india",
                                                        customer_name=sender_name2,
                                                        org_id=org_id,
                                                        source="webhook",
                                                        message_id=wa_message_id,
                                                    )
                                                    if not await is_effective_ai_enabled(org_id, conversation_id):
                                                        return {"status": "ok"}
                                                    await send_whatsapp_message({
                                                        "conversation_id": conversation_id,
                                                        "content": chatbot_response["final_response"],
                                                        "sender_id": PARIJAT_ACADEMY_ORG_WA_ID,
                                                        "ai_usage": chatbot_response.get("usage"),
                                                        }, org_id, "ai", temp_id=f"ai:{wa_message_id}")
                                                except AIProviderUnavailable as exc:
                                                    await _handle_ai_provider_failure(
                                                        exc,
                                                        org_id=org_id,
                                                        conversation_id=conversation_id,
                                                        platform="whatsapp",
                                                        customer_name=sender_name2,
                                                        customer_number=customer_phone_no,
                                                        source_message_id=wa_message_id,
                                                    )
                                                    return {"status": "ok"}

                                return {"status":"ok"}
                                
                            elif message_type == "unsupported":
                                content = "[Unsupported message type]"
                            elif message_type == "reaction":
                                continue  # Handled above
                            else:
                                content = f"[Unsupported message type: {message_type}]"
                           
                            logger.info(f"WhatsApp Message Received from {sender_name2}, Message: {content}")
                            
                            # Create or update conversation
                            conversation_id = await create_or_update_whatsapp_conversation(
                                org_id=org_id,
                                recipient_id=whatsapp_business_id,
                                customer_phone_no=customer_phone_no,
                                customer_name=sender_name2,
                                last_message=content,
                                last_sender="customer",
                                reply_window_expiry=reply_window_expiry_time_utc,
                                is_ad_referral=is_ad_referral,
                                mode="reply",
                                business_phone_id=business_phone_id,
                                business_phone=business_phone,
                            )
                            
                            # Store in WhatsApp-specific collections
                            message_id = await store_whatsapp_message(
                                org_id=org_id,
                                content=content,
                                conversation_id=conversation_id,
                                customer_phone_no=customer_phone_no,
                                type=message_type,
                                payload=message.get("payload"),
                                sender_name=sender_name2,
                                recipient_id=whatsapp_business_id,
                                role=MessageRole.CUSTOMER,
                                message_id=message_id,  # Use the ID from the webhook for deduplication
                                raw_data=message,
                                mode="reply",
                                context_type=context_type,
                                context=context,
                                notify_push=True,
                                business_phone_id=business_phone_id,
                            )

                            if await handle_followup_customer_reply(org_id, conversation_id, content):
                                logger.info(f"Follow-up reply intercepted for {conversation_id}")
                                return {"status": "ok"}

                            # Check if conversation exists
                            existing_conversation = await asyncio.to_thread(
                                conversations_collection.find_one,
                                {"conversation_id": conversation_id},
                            )
                            conversation = await asyncio.to_thread(
                                conversations_collection.find_one,
                                {"conversation_id": conversation_id},
                            )

                            if conversation:
                                # Convert ObjectId to string for JSON serialization
                                conversation["_id"] = str(conversation["_id"])
                                # Convert datetime to ISO string format
                                if "timestamp" in conversation:
                                    conversation["timestamp"] = conversation["timestamp"].isoformat()
                                if "last_message_timestamp" in conversation:
                                    conversation["last_message_timestamp"] = conversation["last_message_timestamp"].isoformat()

                            # Broadcast WhatsApp-specific notifications first
                            if not existing_conversation:
                                await broadcast_whatsapp_message(whatsapp_business_id, "new_conversation", {
                                    "conversation": conversation
                                })
                            else:
                                await broadcast_whatsapp_message(whatsapp_business_id, "conversation_updated", {
                                    "conversation": conversation
                                })

                            contact_data = {
                                "whatsapp_id": whatsapp_business_id,
                                "full_name": sender_name2,
                                "phone_number": customer_phone_no,
                                "country": None,
                                "profile_url": None,
                                "conversation_id": conversation_id,
                                "email": None,
                                "categories": []
                                }

                            store_contact_background(background_tasks, org_id, "whatsapp", contact_data)

                            return {"status": "ok"}
    
    if payload["object"] == "instagram":
        if "entry" in payload and len(payload["entry"]) > 0:
            for entry in payload["entry"]:
                ig_account_id = entry.get("id")

                
                # Prithal's code testing
                for changes in entry.get('changes', []):
                    field = changes.get('field')
                    value = changes.get('value')

                    if field == 'comments':
                        await process_comments_events(value, ig_account_id, background_tasks)
                
                for message in entry.get("messaging", []):
                    # Check if this is a deletion event
                    if "message" in message and message["message"].get("is_deleted", False):
                        # Get the message ID that was deleted
                        deleted_message_id = message["message"]["mid"]
                        recipient_id = message["recipient"]["id"]  # Instagram business account ID
                        sender_id = message["sender"]["id"]        # Instagram user ID who deleted the message
                        
                        logger.info(f"Message deletion event detected - mid: {deleted_message_id}")

                        # Find the corresponding Instagram account in our connections
                        instagram_connection = await asyncio.to_thread(
                            db.instagram_connections.find_one,
                            {"instagram_id": recipient_id, "is_active": True},
                            sort=[("last_updated", -1)],
                        )
                        
                        logger.info(f"Instagram Connection: {instagram_connection}")
                        org_id = instagram_connection.get("org_id")
                        logger.info(f"Org_id: {org_id}")

                        # Get the Instagram-specific collection names
                        messages_collection_name = f"messages_{org_id}"
                        conversations_collection_name = f"conversations_{org_id}" 

                        # Check if collection exists
                        if messages_collection_name in await asyncio.to_thread(db.list_collection_names):

                            messages_collection = db[messages_collection_name]
                            conversations_collection = db[conversations_collection_name]  
                            
                            # Find the message to be deleted
                            message_to_delete = await asyncio.to_thread(
                                messages_collection.find_one,
                                {"message_id": deleted_message_id},
                            )
                            
                            if message_to_delete:
                                conversation_id = message_to_delete.get("conversation_id")
                                
                                # Get the conversation
                                conversation = await asyncio.to_thread(
                                    conversations_collection.find_one,
                                    {"conversation_id": conversation_id},
                                )
                                
                                # Check if this was the last message in the conversation
                                was_last_message = False
                                if conversation and conversation.get("last_message_timestamp"):
                                    # Compare timestamps to see if this was the most recent message
                                    msg_time = message_to_delete.get("timestamp")
                                    conv_time = conversation.get("last_message_timestamp")
                                    
                                    # If the deleted message time matches the conversation's last_message_timestamp
                                    if msg_time and abs((msg_time - conv_time).total_seconds()) < 1:  # Within 1 second
                                        was_last_message = True
                                
                                # Delete the message
                                delete_result = await asyncio.to_thread(
                                    messages_collection.delete_one,
                                    {"message_id": deleted_message_id},
                                )
                                
                                # If this was the last message, update the conversation with previous message
                                if was_last_message:
                                    # Find the new most recent message
                                    newest_message = await asyncio.to_thread(
                                        messages_collection.find_one,
                                        {"conversation_id": conversation_id},
                                        sort=[("timestamp", -1)],  # Sort by timestamp descending
                                    )
                                    
                                    if newest_message:
                                        # Update conversation with new last message and timestamp
                                        await asyncio.to_thread(
                                            conversations_collection.update_one,
                                            {"conversation_id": conversation_id},
                                            {"$set": {
                                                "last_message": newest_message.get("content", ""),
                                                "last_message_timestamp": newest_message.get("timestamp")
                                            }}
                                        )
                                    else:
                                        # If no messages left, set default
                                        await asyncio.to_thread(
                                            conversations_collection.update_one,
                                            {"conversation_id": conversation_id},
                                            {"$set": {
                                                "last_message": "(No messages)",
                                                "last_message_timestamp": datetime.now()
                                            }}
                                        )
                                
                                # Get updated conversation for broadcasting
                                conversation = await asyncio.to_thread(
                                    conversations_collection.find_one,
                                    {"id": conversation_id},
                                )
                                
                                # Format for broadcasting
                                if conversation:
                                    # Convert ObjectId to string for JSON serialization
                                    conversation["_id"] = str(conversation["_id"])
                                    # Convert datetime to ISO string format
                                    if "timestamp" in conversation:
                                        conversation["timestamp"] = conversation["timestamp"].isoformat()
                                    if "last_message_timestamp" in conversation:
                                        conversation["last_message_timestamp"] = conversation["last_message_timestamp"].isoformat()

                                    await broadcast_main_ws(
                                        platform_id=recipient_id,
                                        platform_type="instagram",
                                        event_type="message_deleted",
                                        payload={
                                            "message_id": deleted_message_id,
                                            "conversation_id": conversation_id
                                        }
                                    )

                                    await broadcast_main_ws(
                                        platform_id=recipient_id,
                                        platform_type="instagram",
                                        event_type="conversation_updated",
                                        payload={
                                            "conversation": conversation
                                        }
                                    )

                                    # Broadcast both the deletion event and updated conversation
                                    if recipient_id in services.instagram_connections_map and conversation_id:
                                        await broadcast_instagram_message(recipient_id, "message_deleted", {
                                            "message_id": deleted_message_id,
                                            "conversation_id": conversation_id
                                        })

                                    # Update dashboard stats live after deletion
                                    # await broadcast_on_stats_update(org_id)
                                        
                                    await broadcast_instagram_message(recipient_id, "conversation_updated", {
                                        "conversation": conversation
                                    })
                        
                        # Return success early since we've handled the deletion event
                        return {"status": "ok"}
                

                    # Check if the message is a read event
                    if "read" in message:
                        logger.info("Message read event")
                        continue

                    sender_id = message["sender"]["id"]
                    recipient_id = message["recipient"]["id"]
                    timestamp = message["timestamp"]

                    # REACTION TYPE MESSAGES #######################################################################
                    if (message.get("reaction") is not None):

                        message_timestamp = message.get("timestamp")
                        emoji = message.get("reaction").get("emoji")
                        mid = message.get("reaction").get("mid")
                        action = message.get("reaction").get("action")
                        timestamp = datetime.now()

                        key = f"{mid}_{emoji}_{action}"

                        # Check for duplicate messages reactions / if the message ID is already processed
                        if key in services.processed_message_reaction_ids:
                            logger.info("Duplicate message, skipping processing.")
                            continue
                        
                        # Add message ID with the current timestamp
                        services.processed_message_reaction_ids[key] = timestamp

                        # Find the corresponding Instagram account in our connections
                        instagram_connection = await asyncio.to_thread(
                            db.instagram_connections.find_one,
                            {"instagram_id": recipient_id, "is_active": True},
                            sort=[("last_updated", -1)],
                        )

                        if not instagram_connection:
                            logger.error(f"No active Instagram connection found for recipient ID: {recipient_id}")
                            return {"status": "ok"}
                        
                        org_collection = await asyncio.to_thread(
                            db.organizations.find_one,
                            {"ig_id": recipient_id},
                        )

                        if not org_collection:
                            logger.error(f"No active organization found for Instagram ID: {recipient_id}")
                            return {"status": "ok"}

                        org_id = org_collection.get("org_id")

                        if (not org_id):
                            logger.error(f"No active organization ID found for Instagram ID: {recipient_id}")
                            return {"status": "ok"}

                        # get the page access token
                        page_access_token = instagram_connection.get("page_access_token")
                        if not page_access_token:
                            logger.error(f"Page access token not found for recipient ID: {recipient_id}")
                            return {"status": "error", "message": "Page access token not found"}
                        
                        # Get user-specific collection names
                        messages_collection_name = f"messages_{org_id}"
                        conversations_collection_name = f"conversations_{org_id}"
                        
                        # Get or create collections
                        messages_collection = db[messages_collection_name]
                        conversations_collection = db[conversations_collection_name]
                        
                        user_details = await get_ig_username(sender_id, page_access_token)
                        username = user_details.get('username')
                        name = user_details.get('name', username)
                        user = name + " - @" + username

                        logger.info(f"Reaction Event: {user} reacted to message ID {mid} with emoji: {emoji}")

                        if (action == "react"):
                            # Update the message with the reaction info in  database and it for broadcasting
                            try:
                                # Update the message with the reaction info
                                broadcast_message = await asyncio.to_thread(
                                    messages_collection.find_one_and_update,
                                    {"message_id": mid},
                                    {"$set": {
                                        "reaction": {
                                            "emoji": emoji,
                                            "username": username,
                                            "full_name": name,
                                            "profile_picture_url": user_details.get("profile_pic"),
                                            "timestamp": timestamp
                                        }
                                    }}, 
                                    return_document=ReturnDocument.AFTER,
                                )
                                
                                if not broadcast_message:
                                    logger.warning(f"Message with ID {mid} not found for reaction update")
                                    return {"status": "ok"}
                                    
                            except Exception as e:
                                logger.error(f"Error updating message with reaction: {e}")
                                return {"status": "error", "message": str(e)}
                            
                        elif (action == "unreact"):
                            # Update the message with the reaction info in  database and it for broadcasting
                            try:
                                # Update the message with the reaction info
                                broadcast_message = await asyncio.to_thread(
                                    messages_collection.find_one_and_update,
                                    {"message_id": mid},
                                    {"$unset": {"reaction": 1}}, 
                                    return_document=ReturnDocument.AFTER,
                                )
                                
                                if not broadcast_message:
                                    logger.warning(f"Message with ID {mid} not found for reaction update")
                                    return {"status": "ok"}
                                    
                            except Exception as e:
                                logger.error(f"Error updating message with reaction: {e}")
                                return {"status": "error", "message": str(e)}
                        


                        if "_id" in broadcast_message:
                            broadcast_message["_id"] = str(broadcast_message["_id"])

                        try:
                            from services.sales_learning_service import record_reaction
                            await record_reaction(
                                org_id, f"instagram_{sender_id}",
                                f"ig:{mid}:{sender_id}:{message_timestamp}:{action}:{emoji}",
                                str(mid), str(emoji or ""), action, "instagram",
                            )
                        except Exception as learning_error:
                            logger.warning(f"Could not record internal Instagram reaction learning: {type(learning_error).__name__}")

                        await broadcast_main_ws(
                            platform_id=recipient_id,
                            platform_type="instagram",
                            event_type="message_reaction",
                            payload={"message": broadcast_message}
                            )

                        # Broadcast the reaction update to connected clients
                        if recipient_id in services.instagram_connections_map:
                            logger.success(f"Broadcasting message_reaction event to Frontend for {recipient_id}")
                            await broadcast_instagram_message(recipient_id, "message_reaction", {
                                "message": broadcast_message
                            })

                        return {"status": "ok"}


                    message = message.get("message")
                    
                    if (message is None):
                        logger.warning("No message content, skipping.")
                        return {"status": "ok"}

                    # if no text content in the message, skip processing it
                    if (message.get("text") is None and message.get("attachments") is None):
                        logger.warning("Event can't be handled (not supported), skipping processing.")
                        return {"status": "ok"}
                    
                    # Check for duplicate messages / if the message ID is already processed
                    ig_message_id = message["mid"]

                    message_echo = message.get("is_echo", False)
                    
                    if message_echo:
                        recipient_id, sender_id = sender_id, recipient_id

                    org_collection = await asyncio.to_thread(
                        db.organizations.find_one,
                        {"ig_id": recipient_id},
                    )

                    if not org_collection:
                        logger.error(f"No active organization found for Instagram ID: {recipient_id}")
                        return {"status": "ok"}

                    org_id = org_collection.get("org_id")

                    if (not org_id):
                        logger.error(f"No active organization ID found for Instagram ID: {recipient_id}")
                        return {"status": "ok"}

                    # Resolve credentials only after the account's organization is known.
                    instagram_connection = await asyncio.to_thread(
                        db.instagram_connections.find_one,
                        {"org_id": org_id, "instagram_id": recipient_id, "is_active": True},
                        sort=[("last_updated", -1)],
                    )

                    if not instagram_connection:
                        logger.error(f"No active Instagram connection found for recipient ID: {recipient_id}")
                        return {"status": "ok"}

                    # get the page access token
                    page_access_token = instagram_connection.get("page_access_token")
                    if not page_access_token:
                        logger.error(f"Page access token not found for recipient ID: {recipient_id}")
                        return {"status": "error", "message": "Page access token not found"}
                    
                    # ATTACHMENT TYPE MESSAGES #######################################################################
                    attachment_type = "text"
                    attachment_payload = None

                    context_type = None
                    context = None

                    if (message.get("attachments") is not None):
                        logger.info("Message has attachments")
                        for attachment in message.get("attachments"):
                            attachment_type = str(attachment.get("type"))
                            attachment_payload = attachment.get("payload")

                    # Handle reply type messages 
                    if(message.get("reply_to") is not None):
                        # Handle message replies
                        if message.get("reply_to").get("mid") is not None:
                            logger.info(f"Message is a reply to message ID: {message.get('reply_to').get('mid')}")
                            context_type = "message_reply"
                            context = {
                                "mid": message.get("reply_to").get("mid")
                            }
                            
                        # Handle story replies
                        if message.get("reply_to").get("story") is not None:
                            story_id = message.get("reply_to").get("story").get("id")
                            story_url = message.get("reply_to").get("story").get("url")
                            story_owner = await get_ig_media_owner(story_id, page_access_token)
                            
                            logger.info(f"Message is a reply to story ID: {story_id}")

                            attachment_type = "story_reply"
                            attachment_payload = {
                                "story_media_id": story_id,
                                "story_media_url": story_url,
                                "owner_username": story_owner.get("username"),
                                "owner_profile_picture_url" : story_owner.get("profile_picture_url")
                            }

                    message_text = message.get("text") or {
                        "share": "Shared a post",
                        "ig_story": "Shared a story",
                        "ig_reel": "Shared a reel",
                        "story_mention": "Mentioned you in their story",
                        "image": "Shared an image",
                        "video": "Shared a video",
                    }.get(attachment_type, f"Shared a {attachment_type}")

                    # Get user-specific collection names
                    messages_collection_name = f"messages_{org_id}"
                    conversations_collection_name = f"conversations_{org_id}"
                    
                    # Get or create collections
                    messages_collection = db[messages_collection_name]
                    conversations_collection = db[conversations_collection_name]

                    # Get user details
                    user_details = await get_ig_username(sender_id, page_access_token)
                    username = user_details.get('username')
                    name = user_details.get('name', username)
                    user = ""
                    if name is not None:
                        user = name
                    if username is not None:
                        user = user + " - @" + username

                    logger.info(f"Message Received from {user}, Message: {message_text}")

                    webhook_claim_token = await asyncio.to_thread(
                        claim_instagram_webhook_message,
                        org_id,
                        ig_message_id,
                    )
                    downstream_claim_token = webhook_claim_token
                    if not webhook_claim_token:
                        existing_message = await asyncio.to_thread(
                            messages_collection.find_one,
                            {"id": ig_message_id},
                        )
                        if existing_message and existing_message.get("queue_sync_pending"):
                            try:
                                await sync_instagram_outbound_conversation(
                                    org_id=org_id,
                                    conversation_id=existing_message["conversation_id"],
                                    instagram_id=recipient_id,
                                    message_id=ig_message_id,
                                )
                            except Exception:
                                logger.exception("Could not reconcile duplicate Instagram echo")
                        downstream_claim_token = await asyncio.to_thread(
                            claim_instagram_webhook_downstream,
                            org_id,
                            ig_message_id,
                        )
                        if not downstream_claim_token:
                            logger.info(f"Duplicate Instagram message ignored: {ig_message_id}")
                            continue
                        conversation_id = existing_message.get("conversation_id")
                        message_id = ig_message_id

                    if webhook_claim_token:
                        logger.success("Calling function create_or_update_instagram_conversation")

                        # Create or update conversation only for the first delivery.
                        try:
                            conversation_id = await create_or_update_instagram_conversation(
                                org_id=org_id,
                                recipient_id=recipient_id,
                                sender_id=sender_id,
                                customer_name=name,
                                username=username,
                                last_message=message_echo and f"You: {message_text}" or message_text,
                                last_sender=message_echo and MessageRole.AGENT or MessageRole.CUSTOMER,
                            )

                            # Store in Instagram-specific collections
                            message_id = await store_instagram_message(
                                org_id=org_id,
                                content=message_text,
                                type=attachment_type,
                                payload=attachment_payload,
                                conversation_id=conversation_id,
                                sender_id=sender_id,
                                sender_name=name,
                                sender_username=username,
                                recipient_id=recipient_id,
                                role=message_echo and MessageRole.AGENT or MessageRole.CUSTOMER,
                                message_id=ig_message_id,
                                context_type=context_type,
                                context=context,
                                notify_push=not message_echo,
                                claim_token=webhook_claim_token,
                            )
                        except Exception:
                            await asyncio.to_thread(
                                release_instagram_webhook_claim,
                                org_id,
                                ig_message_id,
                                webhook_claim_token,
                            )
                            raise

                    downstream_state = await asyncio.to_thread(
                        messages_collection.find_one,
                        {"id": ig_message_id},
                    ) or {}
                    if (
                        not message_echo
                        and isinstance(message_text, str)
                        and not downstream_state.get("followup_processed")
                    ):
                        intercepted = await handle_followup_customer_reply(org_id, conversation_id, message_text)
                        await asyncio.to_thread(
                            messages_collection.update_one,
                            {"id": ig_message_id, "webhook_claim_token": downstream_claim_token},
                            {"$set": {"followup_processed": True}},
                        )
                        if intercepted:
                            logger.info(f"Follow-up reply intercepted for {conversation_id}")
                            await asyncio.to_thread(
                                complete_instagram_webhook_message,
                                org_id,
                                ig_message_id,
                                downstream_claim_token,
                            )
                            return {"status": "ok"}
                    
                    if not message_echo:
                        # Get conversation doc to check if AI is enabled
                        convo_doc = await asyncio.to_thread(
                            conversations_collection.find_one,
                            {"conversation_id": conversation_id},
                        )
                        is_conv_ai_enabled = convo_doc.get("is_ai_enabled", False) if convo_doc else True
                        
                        org_doc = await asyncio.to_thread(
                            db.organizations.find_one,
                            {"org_id": org_id},
                            {"is_ai_enabled": 1},
                        )
                        is_org_ai_enabled = org_doc.get("is_ai_enabled", False) if org_doc else False
                        
                        is_heidelai_ig = recipient_id == HEIDELAI_ORG_IG_ID
                        logger.info(f"recipient_id: {recipient_id}")

                        is_production = ENVIRONMENT == "production"
                        is_dev = ENVIRONMENT == "development"
                        is_test = ENVIRONMENT == "test"

                        # fetch org + convo flags
                        ai_enabled = is_org_ai_enabled and is_conv_ai_enabled

                        logger.info(f"is_heidelai_ig: {is_heidelai_ig}")

                        logger.info(f"is_production: {is_production}")
                        logger.info(f"is_dev: {is_dev}")
                        logger.info(f"is_test: {is_test}")
                        logger.info(f"ai_enabled: {ai_enabled}") 

                        if ai_enabled and is_dev and not downstream_state.get("ai_processed"):
                            ai_processing_complete = False
                            try:
                                if await is_effective_ai_enabled(org_id, conversation_id):
                                    chatbot_response = await send_to_agent(
                                        conversation_id=conversation_id,
                                        user_input=message_text,
                                        persona=convo_doc.get("persona", "fallback") if convo_doc else "fallback",
                                        location=convo_doc.get("location", "india") if convo_doc else "india",
                                        customer_name=name,
                                        org_id=org_id,
                                        source="webhook",
                                        message_id=ig_message_id,
                                        message_type=attachment_type,
                                    )
                                    if await is_effective_ai_enabled(org_id, conversation_id):
                                        sales_action = chatbot_response.get("sales_action")
                                        ai_result = await send_ig_message(
                                            id=sender_id,
                                            message_text=chatbot_response["final_response"],
                                            instagram_id=recipient_id,
                                            mode="reply",
                                            org_id=org_id,
                                            temp_id=f"ai:{ig_message_id}",
                                            conversation_id=conversation_id,
                                            sender_name="AI",
                                            enforce_ai_state=True,
                                            ai_usage=chatbot_response.get("usage"),
                                            bot_context={
                                                "sales_action_job_id": sales_action.get("job_id")
                                            } if sales_action else None,
                                        )
                                        if ai_result.get("local_sync_pending"):
                                            raise HTTPException(
                                                status_code=503,
                                                detail="Instagram delivery is still being synchronized",
                                            )
                                        if not ai_result.get("success"):
                                            raise AIProviderUnavailable(
                                                "delivery_unknown"
                                                if ai_result.get("delivery_unknown")
                                                else "delivery_error"
                                            )
                                        if ai_result.get("sales_action_job_id") != (
                                            sales_action.get("job_id")
                                            if sales_action else None
                                        ):
                                            raise AIProviderUnavailable(
                                                "sales_action_delivery_conflict"
                                            )
                                        await mark_sales_action_delivered(
                                            org_id, sales_action, ai_result, platform="instagram",
                                        )
                                else:
                                    await _send_quota_handoff(org_id=org_id, conversation_id=conversation_id, platform="instagram", customer_number=sender_id, customer_name=name or username)
                                ai_processing_complete = True
                            except AIProviderUnavailable as exc:
                                try:
                                    await _handle_ai_provider_failure(
                                        exc,
                                        org_id=org_id,
                                        conversation_id=conversation_id,
                                        platform="instagram",
                                        customer_name=name or username,
                                        customer_number=sender_id,
                                        source_message_id=ig_message_id,
                                    )
                                except Exception:
                                    await asyncio.to_thread(
                                        release_instagram_webhook_downstream,
                                        org_id,
                                        ig_message_id,
                                        downstream_claim_token,
                                    )
                                    raise
                                ai_processing_complete = True
                            except Exception:
                                await asyncio.to_thread(
                                    release_instagram_webhook_downstream,
                                    org_id,
                                    ig_message_id,
                                    downstream_claim_token,
                                )
                                raise
                            finally:
                                if ai_processing_complete:
                                    try:
                                        finalized = await asyncio.to_thread(
                                            messages_collection.update_one,
                                            {"id": ig_message_id, "webhook_claim_token": downstream_claim_token},
                                            {"$set": {"ai_processed": True}},
                                        )
                                        if not finalized.matched_count:
                                            raise RuntimeError("Instagram AI downstream claim was replaced")
                                    except Exception:
                                        await asyncio.to_thread(
                                            release_instagram_webhook_downstream,
                                            org_id,
                                            ig_message_id,
                                            downstream_claim_token,
                                        )
                                        raise

                        if background_tasks and not downstream_state.get("automation_queued"):
                            await asyncio.to_thread(
                                messages_collection.update_one,
                                {"id": ig_message_id, "webhook_claim_token": downstream_claim_token},
                                {"$set": {"automation_queued": True}},
                            )
                            background_tasks.add_task(
                                check_and_trigger_automations,
                                org_id=org_id,
                                platform="instagram",
                                event_type="message_received",
                                trigger_data={
                                    "platform": "instagram",    
                                    "platform_id": recipient_id,
                                    "conversation_id": conversation_id,
                                    "customer_id": sender_id,
                                    "customer_name": name,
                                    "customer_username": username,
                                    "message_text": message_text,
                                    "message_type": attachment_type,
                                    "message_id": ig_message_id
                                }
                            )
                        elif not background_tasks:
                            logger.warning("No background_tasks provided, skipping automation trigger for message_received.")

                    # Check if conversation exists
                    existing_conversation = await asyncio.to_thread(
                        conversations_collection.find_one,
                        {"conversation_id": conversation_id},
                    )
                    conversation = await asyncio.to_thread(
                        conversations_collection.find_one,
                        {"conversation_id": conversation_id},
                    )

                    if recipient_id in services.instagram_connections_map:
                        for connection_key in services.instagram_connections_map[recipient_id]:
                            # Notify each connected client for this Instagram ID
                            try:
                                if connection_key in manager.active_connections:
                                    await manager.active_connections[connection_key].send_json({
                                        "type": "new_instagram_message",
                                        "message": {
                                            "conversation_id": conversation_id,
                                            "sender_id": username,
                                            "content": message_text,
                                            "type": attachment_type,
                                            "payload": attachment_payload,
                                            "timestamp": datetime.now().isoformat(),
                                            "role": message_echo and MessageRole.AGENT or MessageRole.CUSTOMER,
                                        }
                                    })
                            except Exception as e:
                                logger.error(f"Error notifying connection {connection_key}: {e}")
                    
                    if conversation:
                    # Convert ObjectId to string for JSON serialization
                        conversation["_id"] = str(conversation["_id"])
                        # Convert datetime to ISO string format
                        if "timestamp" in conversation:
                            conversation["timestamp"] = conversation["timestamp"].isoformat()
                        if "last_message_timestamp" in conversation:
                            conversation["last_message_timestamp"] = conversation["last_message_timestamp"].isoformat()
                    
                    # Broadcast Instagram-specific notifications first
                    if not existing_conversation:
                        await broadcast_instagram_message(recipient_id, "new_conversation", {
                            "conversation": conversation
                        })
                    else:
                        await broadcast_instagram_message(recipient_id, "conversation_updated", {
                            "conversation": conversation
                        })

                    if not message_echo:
                        contact_data = {
                            "instagram_id": sender_id,
                            "full_name": name,
                            "username": username,
                            "country": None,
                            "profile_url": user_details.get("profile_pic"),
                            "conversation_id": f"instagram_{username}",
                            "email": None,
                            "categories": []
                        }

                        store_contact_background(background_tasks, org_id, "instagram", contact_data)

                    await asyncio.to_thread(
                        complete_instagram_webhook_message,
                        org_id,
                        ig_message_id,
                        downstream_claim_token,
                    )
                    return {"status": "ok"}
                    # Broadcast the new message to Instagram-specific clients
                    # await broadcast_instagram_message(recipient_id, "new_message", {
                    #     "message": {
                    #         "role": "customer",
                    #         "conversation_id": conversation_id,
                    #         "sender_id": username,
                    #         "content": message_text,
                    #         "timestamp": datetime.now().isoformat()
                    #     }
                    # })

    return {"status": "ok"}
        
@router.post("/cashfree")
async def cashfree_webhook(request: Request):

    await verify_cashfree_signature(request)

    raw_body = await request.body()
    payload = json.loads(raw_body)
    logger.info("Cashfree webhook received")


    event_type = payload["type"]
    data = payload["data"]

    idempotency_key = await build_idempotency_key(event_type, data)
    attempt_time = datetime.utcnow()

    try:
        db.cashfree_webhook_events.insert_one({
            "idempotency_key": idempotency_key,
            "gateway": "CASHFREE",
            "event_type": event_type,
            "reference_id": data.get("cf_payment_id")
                or data.get("cf_subscription_id")
                or data.get("link_id"),
            "processed": False,
            "processing": True,
            "attempts": 1,
            "payload": payload,
            "created_at": attempt_time,
            "updated_at": attempt_time,
        })
    except DuplicateKeyError:
        claimed = db.cashfree_webhook_events.find_one_and_update(
            {
                "idempotency_key": idempotency_key,
                "processed": False,
                "$or": [
                    {"processing": {"$ne": True}},
                    {"updated_at": {"$lt": attempt_time - timedelta(minutes=5)}},
                ],
            },
            {
                "$set": {"processing": True, "updated_at": attempt_time, "payload": payload},
                "$inc": {"attempts": 1},
                "$unset": {"last_error": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            return {"status": "duplicate_ignored"}

    try:
        logger.info(f"Processing Cashfree event: {event_type}")
        await process_cashfree_event(event_type, payload)

        db.cashfree_webhook_events.update_one(
            {"idempotency_key": idempotency_key},
            {"$set": {"processed": True, "processing": False, "updated_at": datetime.utcnow()}}
        )

    except Exception as e:
        logger.error(f"Webhook processing failed : {e}")
        db.cashfree_webhook_events.update_one(
            {"idempotency_key": idempotency_key},
            {"$set": {
                "processing": False,
                "last_error": str(e)[:500],
                "updated_at": datetime.utcnow(),
            }},
        )
        raise HTTPException(status_code=500, detail="Cashfree event processing failed") from e

    return {"status": "processed"}
