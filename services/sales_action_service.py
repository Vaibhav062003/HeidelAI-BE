"""Execute the small set of sales actions that V5 is allowed to request."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from config.settings import PRICING_FLOOR_MAX_AGE_MINUTES
from database import get_async_mongo_db
from services.pricing_decision_service import get_pricing_settings
from services.pricing_policy import PriceSnapshot, QuoteCandidate, decide_pricing_v1
from services.pricing_service import _floor_tax_basis, floor_snapshot
from services.quote_service import build_quote_document
from services.sales_case_service import sales_action_job_id, set_sales_action_state


class SalesActionBlocked(Exception):
    def __init__(self, customer_message: str, *, escalate: bool = False):
        super().__init__(customer_message)
        self.customer_message = customer_message
        self.escalate = escalate


_BILL_REQUEST = re.compile(
    r"\b(?:please\s+bill|(?:bill|invoice)\s*(?:karo|kara|kara\s+do|kar\s+do|bana\s+do|banao|bhejo|send|create|raise|generate)|"
    r"(?:send|create|raise|generate)\s+(?:the\s+)?(?:bill|invoice))\b|"
    r"(?:बिल|इनवॉइस)\s*(?:बना\s*दो|बनाओ|भेजो)", re.I,
)
_BILL_NEGATION = re.compile(
    r"\b(?:mat|nahi|nahin|cancel|old|previous|duplicate|copy|do\s+not|don['’]?t|not)\b|(?:मत|नहीं)",
    re.I,
)
_BARE_SEND = frozenset({"bhejo", "send", "send it", "भेजो"})


def is_invoice_draft_intent(text: str, *, has_source: bool = False) -> bool:
    value = " ".join(str(text or "").casefold().split())
    if not value or _BILL_NEGATION.search(value):
        return False
    if value in _BARE_SEND:
        return has_source
    if not _BILL_REQUEST.search(value):
        return False
    return "?" not in value or bool(re.search(
        r"\b(?:can|could|will|would)\s+you\s+(?:please\s+)?(?:send|create|raise)", value,
    ))


def _fact(case: dict[str, Any], field: str) -> Any:
    value = (case.get("facts") or {}).get(field)
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _name(product: dict[str, Any]) -> str:
    return str(
        product.get("product_name") or product.get("model_name")
        or product.get("name") or product.get("title") or "Product"
    )


def _sku(product: dict[str, Any]) -> str:
    return str(next((product.get(key) for key in (
        "unique_identifier", "model_no", "sku", "sku_full", "product_id",
    ) if product.get(key)), ""))


def _stock_count(product: dict[str, Any]) -> int | None:
    for key in ("stock", "stock_available", "quantity", "closing_quantity"):
        value = product.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
        if isinstance(value, str):
            cleaned = value.replace(",", "").strip()
            if cleaned.isdigit():
                return int(cleaned)
    return None


async def _product(org_id: str, selected_id: str) -> dict[str, Any] | None:
    db = get_async_mongo_db()
    fields = ("product_id", "unique_identifier", "model_no", "sku", "sku_full")

    def query(values: set[str]) -> dict[str, Any]:
        return {"$or": [
            {field: {"$regex": f"^{re.escape(value)}$", "$options": "i"}}
            for field in fields for value in values
        ]}

    selected = await db[f"product_{org_id}"].find_one(query({selected_id}))
    identifiers = {selected_id, *(
        str(selected.get(field)).strip() for field in fields
        if selected and selected.get(field)
    )}
    live = await db[f"products_{org_id}"].find(query(identifiers)).limit(2).to_list(length=2)
    return live[0] if len(live) == 1 else None


async def _checked_product(case: dict[str, Any]) -> tuple[dict[str, Any], int, float]:
    selected_id = str(_fact(case, "selected_product_id") or "")
    try:
        quantity = int(_fact(case, "quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    if not selected_id or not 1 <= quantity <= 9999:
        raise SalesActionBlocked("I still need the exact product and quantity before I can continue.")
    organization = await get_async_mongo_db().organizations.find_one(
        {"org_id": case["org_id"]}, {"sales_catalogue_org_id": 1},
    ) or {}
    catalogue_org_id = str(organization.get("sales_catalogue_org_id") or case["org_id"])
    if not re.fullmatch(r"[A-Za-z0-9_-]+", catalogue_org_id):
        raise SalesActionBlocked("The sales catalogue configuration is invalid.", escalate=True)
    product = await _product(catalogue_org_id, selected_id)
    if not product:
        raise SalesActionBlocked(
            "I could not match that product to the current catalogue. Please share its model number.",
            escalate=True,
        )
    stock = _stock_count(product)
    if stock is None:
        raise SalesActionBlocked(
            "I need to verify the live stock count before I promise this order.", escalate=True,
        )
    if stock < quantity:
        raise SalesActionBlocked(
            f"I can currently confirm only {stock} unit{'s' if stock != 1 else ''}. "
            "I can suggest another option or work with a smaller quantity."
        )
    floor, reason = floor_snapshot(product)
    if floor is None:
        raise SalesActionBlocked(
            "I need to refresh the approved price before I send this quote.", escalate=True,
        )
    logger.info(f"Sales price checked from catalogue: {reason}")
    return product, quantity, floor


async def _sales_action_quote_price(
    case: dict[str, Any],
    product: dict[str, Any],
    quantity: int,
    legacy_floor: float,
    source_message_id: str,
) -> float:
    """Audit shadow decisions and enforce the exact target policy only in review."""
    try:
        pricing_mode = str(
            (await get_pricing_settings(str(case["org_id"]))).get("mode") or "off"
        )
    except Exception as exc:
        raise SalesActionBlocked(
            "I need to verify the active pricing policy before I send this quote.",
            escalate=True,
        ) from exc
    if pricing_mode == "off":
        return legacy_floor

    now = datetime.now(timezone.utc)
    decision_data: dict[str, Any]
    target_price: float | None = None
    try:
        snapshot = PriceSnapshot.from_product(product)
        candidate_amount = snapshot.target_selling_price or snapshot.approved_floor_price or legacy_floor
        candidate = QuoteCandidate(
            amount=candidate_amount,
            sku=snapshot.sku,
            currency=snapshot.currency,
            gst_inclusive=snapshot.selling_gst_inclusive,
            gst_rate=snapshot.gst_rate,
            quantity=quantity,
            structured=True,
            exact_product_match=True,
            actor="ai",
        )
        decision = decide_pricing_v1(
            snapshot,
            candidate,
            now=now,
            max_age=timedelta(minutes=PRICING_FLOOR_MAX_AGE_MINUTES),
        )
        target_price = (
            float(snapshot.target_selling_price)
            if snapshot.target_selling_price is not None
            else None
        )
        decision_data = {
            "action": decision.action,
            "reasons": list(decision.reasons),
            "quote_inr_gst_inclusive": (
                str(decision.quote_inr_gst_inclusive)
                if decision.quote_inr_gst_inclusive is not None else None
            ),
            "floor_inr_gst_inclusive": (
                str(decision.floor_inr_gst_inclusive)
                if decision.floor_inr_gst_inclusive is not None else None
            ),
            "target_inr_gst_inclusive": (
                str(decision.target_inr_gst_inclusive)
                if decision.target_inr_gst_inclusive is not None else None
            ),
            "price_version": decision.price_version,
        }
    except (TypeError, ValueError) as exc:
        decision_data = {
            "action": "review_required",
            "reasons": ["invalid_pricing_snapshot"],
            "detail": str(exc),
            "price_version": product.get("price_version"),
        }

    audit_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{case['org_id']}:{case['case_id']}:{source_message_id}:sales_action_pricing",
    ))
    audit = {
        "_id": audit_id,
        "schema_version": "sales_action_pricing_decision.v1",
        "org_id": str(case["org_id"]),
        "conversation_id": str(case["conversation_id"]),
        "case_id": str(case["case_id"]),
        "source_message_id": source_message_id,
        "mode": pricing_mode,
        "source": "sales_action_create_quote",
        "quantity": quantity,
        "sku": _sku(product),
        "legacy_floor_price": legacy_floor,
        **decision_data,
        "created_at": now,
        "expires_at": now + timedelta(days=30),
    }
    try:
        await get_async_mongo_db()[f"pricing_decisions_{case['org_id']}"].update_one(
            {"_id": audit_id}, {"$setOnInsert": audit}, upsert=True,
        )
    except Exception as exc:
        if pricing_mode == "review":
            raise SalesActionBlocked(
                "I verified the price but could not save its pricing audit. Please ask the sales team to review it.",
                escalate=True,
            ) from exc
        logger.error(f"Could not persist shadow sales-action pricing audit: {exc}")

    if pricing_mode == "shadow":
        return legacy_floor
    if decision_data["action"] != "auto_send" or target_price is None:
        reasons = ", ".join(decision_data.get("reasons") or ["pricing_review_required"])
        raise SalesActionBlocked(
            f"I need the sales team to review this quote before I send it ({reasons}).",
            escalate=True,
        )
    return target_price


async def _create_quote(case: dict[str, Any], source_message_id: str) -> dict[str, Any]:
    from services.entitlement_service import require_feature

    require_feature(str(case["org_id"]), "OFFERS")
    product, quantity, unit_price = await _checked_product(case)
    unit_price = await _sales_action_quote_price(
        case, product, quantity, unit_price, source_message_id,
    )
    db = get_async_mongo_db()
    settings = await db[f"quote_settings_{case['org_id']}"].find_one({"_id": "branding"}) or {}
    seller = dict(settings.get("seller") or {})
    organization = await db.organizations.find_one(
        {"org_id": case["org_id"]}, {"org_name": 1},
    ) or {}
    seller["name"] = str(seller.get("name") or organization.get("org_name") or "").strip()
    if not str(seller.get("name") or "").strip():
        raise SalesActionBlocked(
            "Your quote details are ready, but the seller profile must be completed before I can issue it.",
            escalate=True,
        )
    conversation = await db[f"conversations_{case['org_id']}"].find_one(
        {"conversation_id": case["conversation_id"]},
        {"customer_name": 1, "customer_phone_no": 1, "phone": 1, "email": 1},
    ) or {}
    now = datetime.now(timezone.utc)
    tax_basis = _floor_tax_basis(product)
    try:
        gst_rate = float(str(product.get("gst_rate")).replace("%", "").strip())
    except (TypeError, ValueError):
        gst_rate = -1
    if tax_basis not in {"included", "extra"} or not 0 <= gst_rate <= 100:
        raise SalesActionBlocked(
            "I need to verify the product GST details before I send this quote.", escalate=True,
        )
    validity_days = 7
    case_short = re.sub(r"[^A-Za-z0-9]", "", str(case["case_id"]))[-6:].upper()
    quote_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{case['org_id']}:{case['case_id']}:{source_message_id}:quote"))
    quote_number = f"AI-{now:%Y%m%d}-{case_short}-{quote_id[:6].upper()}"
    payload = {
        "conversation_id": case["conversation_id"], "quote_number": quote_number,
        "issued_on": date.today().isoformat(), "due_date": None,
        "valid_until": (date.today() + timedelta(days=validity_days)).isoformat(),
        "seller": {**seller, "logo_url": settings.get("logo_url", "")},
        "customer": {
            "name": conversation.get("customer_name") or case.get("customer_name") or "Customer",
            "phone": conversation.get("customer_phone_no") or conversation.get("phone") or "",
            "email": conversation.get("email") or "", "gstin": "", "address": "",
            "payment_details": "", "logo_url": "",
        },
        "items": [{
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{quote_id}:item")),
            "description": _name(product), "category": str(product.get("category") or ""),
            "sku": _sku(product), "quantity": quantity, "unit_price": unit_price,
            "gst_rate": gst_rate, "gst_inclusive": tax_basis == "included",
            "image_url": str(product.get("image_url") or ""),
        }],
        "shipping": 0, "discount": 0,
        "notes": "Price and stock were checked from the current product catalogue.",
        "terms": list(settings.get("terms") or [
            "Quotation is valid until the date shown above.",
            "Warranty is provided as per the manufacturer policy.",
        ])[:20],
        "public_options": {
            "show_unit_prices": True, "show_line_totals": True,
            "show_quantities": True, "show_sku": True,
            "show_gst_breakdown": True, "show_item_images": True,
        },
        "branding": {"document_title": "Quotation", "build_title": "",
                     "build_label": "", "build_promise": "GST invoice",
                     "theme": "slate", "cover_image_url": "",
                     "footer_text": "Thank you for your business.",
                     "component_categories": [], "design": None},
    }
    document = {
        **build_quote_document(
            payload, org_id=str(case["org_id"]), actor_id="ai-sales",
            actor_name="AI Sales Agent",
        ),
        "quote_id": quote_id, "source_message_id": source_message_id,
        "created_at": now, "created_by": "ai-sales",
        "created_by_name": "AI Sales Agent", "revision_number": 1,
        "revision_count": 0,
    }
    await db[f"quotes_{case['org_id']}"].update_one(
        {"org_id": case["org_id"], "quote_id": quote_id},
        {"$setOnInsert": document}, upsert=True,
    )
    quote = await db[f"quotes_{case['org_id']}"].find_one({"quote_id": quote_id}) or document
    quote_item = (quote.get("items") or [{}])[0]
    gst = float(quote.get("private_gst_total") or 0)
    return {
        "reply": (
            f"*Quotation {quote['quote_number']}*\n"
            f"{quote_item.get('description') or _name(product)}\n"
            f"Quantity: {quote_item.get('quantity') or quantity}\n"
            f"Unit price: Rs.{float(quote_item.get('private_unit_price') or unit_price):,.2f}\n"
            f"GST: Rs.{gst:,.2f}\n*Total: Rs.{float(quote['public_grand_total']):,.2f}*\n"
            f"Valid until: {quote['valid_until']}\n\nReply *proceed* when you are ready for billing."
        ),
        "fields": {"quote_id": quote_id, "quote_number": quote["quote_number"]},
        "ready_stage": "stock_confirmation", "delivered_stage": "quote_sent",
    }


async def _stock_confirmation(case: dict[str, Any]) -> dict[str, Any]:
    product, quantity, _ = await _checked_product(case)
    return {
        "reply": f"I confirmed {quantity} unit{'s' if quantity != 1 else ''} of {_name(product)} in stock.",
        "fields": {}, "ready_stage": "stock_confirmation",
        "delivered_stage": "stock_confirmation",
    }


async def _invoice_draft(
    case: dict[str, Any], customer_text: str, source_message_id: str,
) -> dict[str, Any]:
    source_ids = [str(value) for value in case.get("action_source_message_ids") or [] if value]
    if not is_invoice_draft_intent(customer_text, has_source=bool(source_ids or case.get("quote_id"))):
        raise SalesActionBlocked("Please confirm which quoted products you want me to send for billing.")
    from services.tally_service import create_tally_invoice_draft
    draft = await create_tally_invoice_draft(
        str(case["org_id"]), "ai-sales", "admin", str(case["conversation_id"]),
        {
            "source": "messages", "source_message_ids": source_ids,
            "idempotency_key": sales_action_job_id(
                str(case["case_id"]), source_message_id, "create_invoice_draft"
            ),
        },
    )
    return {
        "reply": "I have prepared the order and sent it for billing review. The invoice will be shared after Billing confirms it in Tally.",
        "fields": {
            "invoice_draft_id": draft["draft_id"],
            "invoice_source_message_ids": draft.get("source_message_ids") or [],
        },
        "ready_stage": "invoice_draft", "delivered_stage": "invoice_draft",
    }


async def _schedule_followup(case: dict[str, Any]) -> dict[str, Any]:
    from services.followup_service import schedule_followup_message
    scheduled = await schedule_followup_message(
        str(case["org_id"]), str(case["conversation_id"]), source="manual",
        requested_by="ai-sales", requested_by_name="AI Sales Agent",
    )
    if scheduled.get("requires_agent_time"):
        raise SalesActionBlocked(str(scheduled.get("message") or "Please confirm when I should follow up."))
    return {
        "reply": "Done. I will follow up at the right time.",
        "fields": {"followup_id": scheduled.get("id") or scheduled.get("message_id")},
        "ready_stage": "nurture", "delivered_stage": "nurture",
    }


async def _handoff(case: dict[str, Any], source_message_id: str, customer_text: str) -> dict[str, Any]:
    if not re.search(r"\b(?:human|person|agent|manager|executive|sales\s+team|call\s+me|insaan|aadmi)\b", customer_text, re.I):
        raise SalesActionBlocked("I can continue helping with the product, price, quote, and billing here.")
    from services.team_service import create_ai_escalation
    phone = re.sub(r"\D", "", str(case["conversation_id"]))
    handoff_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{case['org_id']}:{case['case_id']}:{source_message_id}:sales-handoff",
    ))
    await create_ai_escalation(str(case["org_id"]), {
        "id": handoff_id, "dedupe_id": f"sales-action:{handoff_id}",
        "user_name": case.get("customer_name") or "Customer",
        "customer_number": phone, "customer_text": customer_text,
        "inferred_intent": "customer_requested_human", "priority": "normal",
        "metadata": {"reason": "customer_requested_human", "case_id": case["case_id"],
                     "source_message_id": source_message_id},
    })
    return {
        "reply": "I have shared this conversation with the sales team. They can see your requirements.",
        "fields": {}, "ready_stage": None, "delivered_stage": None,
    }


async def _job_result_or_pending(jobs, job_id: str) -> dict[str, Any]:
    current = await jobs.find_one({"_id": job_id}) or {}
    if isinstance(current.get("result"), dict):
        return current["result"]
    return {"pending": True, "job_id": job_id}


async def execute_sales_action(
    *, case: dict[str, Any], source_message_id: str, action_type: str,
    customer_text: str, recover_result_only: bool = False,
) -> dict[str, Any] | None:
    if action_type in {"none", "show_products"}:
        return None
    db = get_async_mongo_db()
    job_id = sales_action_job_id(str(case["case_id"]), source_message_id, action_type)
    jobs = db[f"sales_action_jobs_{case['org_id']}"]
    existing = await jobs.find_one({"_id": job_id})
    if existing and isinstance(existing.get("result"), dict):
        return existing.get("result")
    if recover_result_only:
        return {"requires_review": True, "job_id": job_id}
    now = datetime.now(timezone.utc)
    claim_id = str(uuid.uuid4())
    claimed = None
    if not existing:
        try:
            await jobs.insert_one({
                "_id": job_id, "case_id": case["case_id"], "org_id": case["org_id"],
                "conversation_id": case["conversation_id"], "source_message_id": source_message_id,
                "action_type": action_type, "status": "processing", "claim_id": claim_id,
                "created_at": now, "updated_at": now, "attempts": 1,
            })
            claimed = True
        except DuplicateKeyError:
            existing = await jobs.find_one({"_id": job_id})
    if not claimed:
        claimed = await jobs.find_one_and_update(
            {
                "_id": job_id,
                "$or": [
                    {"status": {"$in": ["blocked", "failed"]}},
                    {"status": "processing", "updated_at": {"$lt": now - timedelta(minutes=2)}},
                ],
            },
            {"$set": {"status": "processing", "claim_id": claim_id, "updated_at": now},
             "$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
        )
    if not claimed:
        return {"pending": True, "job_id": job_id}
    await set_sales_action_state(
        org_id=str(case["org_id"]), case_id=str(case["case_id"]),
        source_message_id=source_message_id, action_type=action_type, status="processing",
    )
    try:
        if action_type == "create_quote":
            result = await _create_quote(case, source_message_id)
        elif action_type == "request_stock_confirmation":
            result = await _stock_confirmation(case)
        elif action_type == "create_invoice_draft":
            result = await _invoice_draft(case, customer_text, source_message_id)
        elif action_type == "schedule_followup":
            result = await _schedule_followup(case)
        elif action_type == "create_handoff":
            result = await _handoff(case, source_message_id, customer_text)
        else:
            raise SalesActionBlocked("That sales action is not available yet.")
    except SalesActionBlocked as exc:
        result = {
            "reply": exc.customer_message, "blocked": True, "job_id": job_id,
            "case_id": case["case_id"], "conversation_id": case["conversation_id"],
            "source_message_id": source_message_id, "action_type": action_type,
        }
        if exc.escalate:
            result.update({
                "requires_review": True,
                "review_category": "sales_action_review_required",
            })
        saved = await jobs.find_one_and_update(
            {"_id": job_id, "status": "processing", "claim_id": claim_id},
            {"$set": {
                "status": "blocked", "error": exc.customer_message, "result": result,
                "updated_at": datetime.now(timezone.utc),
            }, "$unset": {"claim_id": ""}},
            return_document=ReturnDocument.AFTER,
        )
        if not saved:
            return await _job_result_or_pending(jobs, job_id)
        try:
            await set_sales_action_state(
                org_id=str(case["org_id"]), case_id=str(case["case_id"]),
                source_message_id=source_message_id, action_type=action_type,
                status="blocked", error=exc.customer_message,
            )
        except Exception:
            result.update({
                "requires_review": True,
                "review_category": "sales_action_review_required",
            })
            await jobs.update_one(
                {"_id": job_id, "status": "blocked"},
                {"$set": {"result": result}},
            )
        return result
    except Exception as exc:
        logger.error(f"Sales action failed: {action_type}: {exc}", exc_info=True)
        result = {
            "reply": "I could not complete that safely. Please contact the sales team.",
            "blocked": True, "job_id": job_id, "case_id": case["case_id"],
            "conversation_id": case["conversation_id"],
            "source_message_id": source_message_id, "action_type": action_type,
            "requires_review": True,
            "review_category": "sales_action_review_required",
        }
        saved = await jobs.find_one_and_update(
            {"_id": job_id, "status": "processing", "claim_id": claim_id},
            {"$set": {
                "status": "failed", "error": str(exc)[:300], "result": result,
                "updated_at": datetime.now(timezone.utc),
            }, "$unset": {"claim_id": ""}},
            return_document=ReturnDocument.AFTER,
        )
        if not saved:
            return await _job_result_or_pending(jobs, job_id)
        try:
            await set_sales_action_state(
                org_id=str(case["org_id"]), case_id=str(case["case_id"]),
                source_message_id=source_message_id, action_type=action_type,
                status="failed", error="The action could not be completed safely.",
            )
        except Exception as state_error:
            logger.error(f"Could not synchronize failed sales action: {state_error}")
        return result

    result.update({"job_id": job_id, "case_id": case["case_id"],
                   "conversation_id": case["conversation_id"],
                   "source_message_id": source_message_id, "action_type": action_type})
    saved = await jobs.find_one_and_update(
        {"_id": job_id, "status": "processing", "claim_id": claim_id},
        {"$set": {
            "status": "ready", "result": result, "updated_at": datetime.now(timezone.utc),
        }, "$unset": {"claim_id": ""}},
        return_document=ReturnDocument.AFTER,
    )
    if not saved:
        return await _job_result_or_pending(jobs, job_id)
    await set_sales_action_state(
        org_id=str(case["org_id"]), case_id=str(case["case_id"]),
        source_message_id=source_message_id, action_type=action_type, status="ready",
        fields=result.get("fields"), stage=result.get("ready_stage"),
    )
    if action_type == "create_invoice_draft":
        try:
            from services.sales_learning_service import record_verified_tally_outcome
            await record_verified_tally_outcome(
                str(case["org_id"]), str(case["conversation_id"]),
                str((result.get("fields") or {}).get("invoice_draft_id") or job_id),
                "invoice_draft",
            )
        except Exception as exc:
            logger.warning(f"Could not record internal invoice-draft learning event: {exc}")
    return result


async def mark_sales_action_delivered(
    org_id: str, action: dict[str, Any] | None,
    delivery_result: dict[str, Any] | None = None, platform: str = "whatsapp",
) -> None:
    if not action or action.get("blocked") or not action.get("job_id"):
        return
    jobs = get_async_mongo_db()[f"sales_action_jobs_{org_id}"]
    job = await jobs.find_one({"_id": action["job_id"]})
    if not job or job.get("status") != "ready":
        return
    await set_sales_action_state(
        org_id=org_id, case_id=str(action["case_id"]),
        source_message_id=str(action["source_message_id"]),
        action_type=str(action["action_type"]), status="completed",
        stage=action.get("delivered_stage"),
    )
    from services.sales_learning_service import record_sales_learning_event
    case = await get_async_mongo_db()[f"sales_cases_{org_id}"].find_one({"case_id": str(action["case_id"])})
    if case:
        await record_sales_learning_event(
            org_id=org_id, event_type="message_delivered",
            conversation_id=str(action.get("conversation_id") or ""),
            source_id=str(action["job_id"]), case=case, channel=platform,
        )
    quote_id = (action.get("fields") or {}).get("quote_id")
    if quote_id and delivery_result:
        from services.sales_offer_service import enqueue_outbound_offer
        await enqueue_outbound_offer(
            org_id=org_id, agent_id="ai-sales", agent_name="AI Sales Agent",
            conversation_id=str(action.get("conversation_id") or ""),
            platform=platform, content=str(action.get("reply") or ""),
            result=delivery_result,
            context={"quote": {"id": quote_id, "revision": 1}},
        )
    if action.get("action_type") == "create_quote":
        from services.followup_service import schedule_followup_message
        await schedule_followup_message(
            org_id, str(action.get("conversation_id") or ""),
            source="quotation",
            quote_id=str(quote_id or (action.get("fields") or {}).get("quote_id") or ""),
            requested_by="ai-sales", requested_by_name="AI Sales Agent",
        )
    await jobs.update_one(
        {"_id": action["job_id"], "status": "ready"},
        {"$set": {"status": "delivered", "delivered_at": datetime.now(timezone.utc)}},
    )
