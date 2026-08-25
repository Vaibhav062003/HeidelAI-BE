"""Execute the small set of sales actions that V5 is allowed to request."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from loguru import logger
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from config.settings import CASHFREE_APP_ID, CASHFREE_SECRET_KEY, CASHFREE_URL, CASHFREE_VERSION
from database import get_async_mongo_db
from services.pricing_service import _floor_tax_basis, floor_snapshot
from services.quote_service import build_quote_document
from services.sales_case_service import sales_action_job_id, set_sales_action_state


class SalesActionBlocked(Exception):
    def __init__(self, customer_message: str, *, escalate: bool = False):
        super().__init__(customer_message)
        self.customer_message = customer_message
        self.escalate = escalate


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
    live = await db[f"products_{org_id}"].find_one(query(identifiers))
    return live or selected


async def _checked_product(case: dict[str, Any]) -> tuple[dict[str, Any], int, float]:
    selected_id = str(_fact(case, "selected_product_id") or "")
    try:
        quantity = int(_fact(case, "quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    if not selected_id or not 1 <= quantity <= 9999:
        raise SalesActionBlocked("I still need the exact product and quantity before I can continue.")
    catalogue_org_id = str(case.get("catalogue_org_id") or "")
    if not catalogue_org_id:
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


async def _create_quote(case: dict[str, Any], source_message_id: str) -> dict[str, Any]:
    from services.entitlement_service import require_feature

    require_feature(str(case["org_id"]), "OFFERS")
    product, quantity, unit_price = await _checked_product(case)
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
    gst = float(quote.get("private_gst_total") or 0)
    return {
        "reply": (
            f"*Quotation {quote_number}*\n"
            f"{_name(product)}\n"
            f"Quantity: {quantity}\nUnit price: Rs.{unit_price:,.2f}\n"
            f"GST: Rs.{gst:,.2f}\n*Total: Rs.{float(quote['public_grand_total']):,.2f}*\n"
            f"Valid until: {quote['valid_until']}\n\nReply *proceed* when you are ready to pay."
        ),
        "fields": {"quote_id": quote_id, "quote_number": quote_number},
        "ready_stage": "stock_confirmation", "delivered_stage": "quote_sent",
    }


async def _stock_confirmation(case: dict[str, Any]) -> dict[str, Any]:
    product, quantity, _ = await _checked_product(case)
    return {
        "reply": f"I confirmed {quantity} unit{'s' if quantity != 1 else ''} of {_name(product)} in stock.",
        "fields": {}, "ready_stage": "stock_confirmation",
        "delivered_stage": "stock_confirmation",
    }


async def _payment_link(case: dict[str, Any], source_message_id: str) -> dict[str, Any]:
    from services.entitlement_service import require_feature

    require_feature(str(case["org_id"]), "OFFERS")
    if case.get("stage") not in {"quote_sent", "payment_pending"} or not case.get("quote_id"):
        raise SalesActionBlocked("I need to send and confirm the quote before creating a payment link.")
    await _checked_product(case)  # Stock and floor must still be current.
    db = get_async_mongo_db()
    quote = await db[f"quotes_{case['org_id']}"].find_one({
        "quote_id": case["quote_id"], "conversation_id": case["conversation_id"],
    })
    if not quote:
        raise SalesActionBlocked("I could not find the saved quote for this payment.", escalate=True)
    existing_url = str(case.get("payment_link_url") or "")
    if case.get("stage") == "payment_pending" and existing_url:
        return {
            "reply": (
                f"Secure payment link for *{quote['quote_number']}*\n"
                f"Amount: *Rs.{float(quote['public_grand_total']):,.2f}*\n{existing_url}\n\n"
                "The order is confirmed only after payment succeeds."
            ),
            "fields": {
                "quote_id": case["quote_id"],
                "payment_order_id": case.get("payment_order_id") or "",
                "payment_link_id": case.get("payment_link_id") or "",
                "payment_link_url": existing_url,
            },
            "ready_stage": "payment_pending", "delivered_stage": "payment_pending",
        }
    if not CASHFREE_URL or not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise SalesActionBlocked(
            "The secure payment link is not configured yet. Your quote remains saved.", escalate=True,
        )
    customer = dict(quote.get("customer") or {})
    phone = re.sub(r"\D", "", str(customer.get("phone") or case["conversation_id"]))[-10:]
    if len(phone) != 10:
        raise SalesActionBlocked(
            "I need a valid 10-digit mobile number before I can create the secure payment link."
        )
    link_id = "sale_" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{case['org_id']}:{case['quote_id']}:{quote.get('revision_number', 1)}",
    ).hex[:32]
    payload = {
        "link_id": link_id, "link_amount": float(quote["public_grand_total"]),
        "link_currency": "INR", "link_purpose": f"Payment for {quote['quote_number']}",
        "customer_details": {
            "customer_name": customer.get("name") or case.get("customer_name") or "Customer",
            "customer_phone": phone,
            **({"customer_email": customer["email"]} if customer.get("email") else {}),
        },
        "link_partial_payments": False, "link_auto_reminders": True,
        "link_notes": {"org_id": str(case["org_id"]), "case_id": str(case["case_id"]),
                       "quote_id": str(case["quote_id"])},
    }
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{link_id}:{source_message_id}"))
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{CASHFREE_URL.rstrip('/')}/links", json=payload,
            headers={
                "x-client-id": CASHFREE_APP_ID, "x-client-secret": CASHFREE_SECRET_KEY,
                "x-api-version": CASHFREE_VERSION or "2025-01-01",
                "x-request-id": request_id, "x-idempotency-key": request_id,
                "Content-Type": "application/json",
            },
        )
    response.raise_for_status()
    result = response.json()
    if not result.get("link_url"):
        raise SalesActionBlocked("Cashfree did not return a payment link. Your quote remains saved.", escalate=True)
    await db.sales_payment_links.update_one(
        {"link_id": link_id},
        {"$setOnInsert": {"org_id": case["org_id"], "case_id": case["case_id"],
                          "quote_id": case["quote_id"], "conversation_id": case["conversation_id"],
                          "amount": float(quote["public_grand_total"]),
                          "cf_link_id": str(result.get("cf_link_id") or ""), "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return {
        "reply": (
            f"Secure payment link for *{quote['quote_number']}*\n"
            f"Amount: *Rs.{float(quote['public_grand_total']):,.2f}*\n{result['link_url']}\n\n"
            "The order is confirmed only after payment succeeds."
        ),
        "fields": {
            "quote_id": case["quote_id"],
            "payment_order_id": link_id, "payment_link_id": str(result.get("cf_link_id") or ""),
            "payment_link_url": result["link_url"],
        },
        "ready_stage": "quote_sent", "delivered_stage": "payment_pending",
    }


async def _handoff(case: dict[str, Any], source_message_id: str, customer_text: str) -> dict[str, Any]:
    from services.team_service import create_ai_escalation
    phone = re.sub(r"\D", "", str(case["conversation_id"]))
    await create_ai_escalation(str(case["org_id"]), {
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


async def _escalate_block(case: dict[str, Any], source_message_id: str, reason: str) -> None:
    try:
        from services.team_service import create_ai_escalation
        await create_ai_escalation(str(case["org_id"]), {
            "user_name": case.get("customer_name") or "Customer",
            "customer_number": re.sub(r"\D", "", str(case["conversation_id"])),
            "customer_text": reason, "inferred_intent": "sales_action_blocked",
            "priority": "normal", "metadata": {
                "reason": "sales_action_blocked", "case_id": case["case_id"],
                "source_message_id": source_message_id,
            },
        })
    except Exception as exc:
        logger.error(f"Could not create blocked sales escalation: {exc}")


async def execute_sales_action(
    *, case: dict[str, Any], source_message_id: str, action_type: str,
    customer_text: str,
) -> dict[str, Any] | None:
    if action_type in {"none", "show_products", "schedule_followup"}:
        return None
    db = get_async_mongo_db()
    job_id = sales_action_job_id(str(case["case_id"]), source_message_id, action_type)
    jobs = db[f"sales_action_jobs_{case['org_id']}"]
    existing = await jobs.find_one({"_id": job_id})
    if existing and existing.get("status") in {"ready", "delivered"}:
        return existing.get("result")
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
        return {"reply": "I am still completing that safely. Please wait a moment.",
                "blocked": True, "job_id": job_id}
    await set_sales_action_state(
        org_id=str(case["org_id"]), case_id=str(case["case_id"]),
        source_message_id=source_message_id, action_type=action_type, status="processing",
    )
    try:
        if action_type == "create_quote":
            result = await _create_quote(case, source_message_id)
        elif action_type == "request_stock_confirmation":
            result = await _stock_confirmation(case)
        elif action_type == "create_payment_link":
            result = await _payment_link(case, source_message_id)
        elif action_type == "create_handoff":
            result = await _handoff(case, source_message_id, customer_text)
        else:
            raise SalesActionBlocked("That sales action is not available yet.")
    except SalesActionBlocked as exc:
        if exc.escalate:
            await _escalate_block(case, source_message_id, exc.customer_message)
        await jobs.update_one({"_id": job_id}, {"$set": {
            "status": "blocked", "error": exc.customer_message, "updated_at": datetime.now(timezone.utc),
        }, "$unset": {"claim_id": ""}})
        await set_sales_action_state(
            org_id=str(case["org_id"]), case_id=str(case["case_id"]),
            source_message_id=source_message_id, action_type=action_type,
            status="blocked", error=exc.customer_message,
        )
        return {"reply": exc.customer_message, "blocked": True, "job_id": job_id}
    except Exception as exc:
        logger.error(f"Sales action failed: {action_type}: {exc}", exc_info=True)
        await jobs.update_one({"_id": job_id}, {"$set": {
            "status": "failed", "error": str(exc)[:300], "updated_at": datetime.now(timezone.utc),
        }, "$unset": {"claim_id": ""}})
        await set_sales_action_state(
            org_id=str(case["org_id"]), case_id=str(case["case_id"]),
            source_message_id=source_message_id, action_type=action_type,
            status="failed", error="The action could not be completed safely.",
        )
        return {"reply": "I could not complete that safely. Your request is saved for review.",
                "blocked": True, "job_id": job_id}

    result.update({"job_id": job_id, "case_id": case["case_id"],
                   "conversation_id": case["conversation_id"],
                   "source_message_id": source_message_id, "action_type": action_type})
    await jobs.update_one({"_id": job_id}, {"$set": {
        "status": "ready", "result": result, "updated_at": datetime.now(timezone.utc),
    }, "$unset": {"claim_id": ""}})
    await set_sales_action_state(
        org_id=str(case["org_id"]), case_id=str(case["case_id"]),
        source_message_id=source_message_id, action_type=action_type, status="ready",
        fields=result.get("fields"), stage=result.get("ready_stage"),
    )
    return result


async def mark_sales_action_delivered(
    org_id: str, action: dict[str, Any] | None,
    delivery_result: dict[str, Any] | None = None, platform: str = "whatsapp",
) -> None:
    if not action or action.get("blocked") or not action.get("job_id"):
        return
    jobs = get_async_mongo_db()[f"sales_action_jobs_{org_id}"]
    result = await jobs.find_one_and_update(
        {"_id": action["job_id"], "status": {"$ne": "delivered"}},
        {"$set": {"status": "delivered", "delivered_at": datetime.now(timezone.utc)}},
    )
    if result is None:
        return
    await set_sales_action_state(
        org_id=org_id, case_id=str(action["case_id"]),
        source_message_id=str(action["source_message_id"]),
        action_type=str(action["action_type"]), status="completed",
        stage=action.get("delivered_stage"),
    )
    quote_id = (action.get("fields") or {}).get("quote_id")
    if quote_id and delivery_result:
        try:
            from services.sales_offer_service import enqueue_outbound_offer
            await enqueue_outbound_offer(
                org_id=org_id, agent_id="ai-sales", agent_name="AI Sales Agent",
                conversation_id=str(action.get("conversation_id") or ""),
                platform=platform, content=str(action.get("reply") or ""),
                result=delivery_result,
                context={"quote": {"id": quote_id, "revision": 1}},
            )
        except Exception as exc:
            logger.error(f"Could not audit delivered AI quote: {exc}")
    if action.get("action_type") in {"create_quote", "create_payment_link"}:
        try:
            from services.followup_service import schedule_followup_message
            await schedule_followup_message(
                org_id, str(action.get("conversation_id") or ""),
                source="payment" if action["action_type"] == "create_payment_link" else "quotation",
                quote_id=str(quote_id or (action.get("fields") or {}).get("quote_id") or ""),
                requested_by="ai-sales", requested_by_name="AI Sales Agent",
            )
        except Exception as exc:
            logger.error(f"Could not schedule AI sales follow-up: {exc}")


async def apply_sales_payment_webhook(payload: dict[str, Any]) -> bool:
    """Mark a deal won only for a full Cashfree success event."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    link_id = data.get("link_id")
    paid = data.get("link_status") == "PAID" and float(data.get("link_amount_paid") or 0) >= float(data.get("link_amount") or 0) > 0
    if not link_id or not paid:
        return False
    db = get_async_mongo_db()
    link = await db.sales_payment_links.find_one({"link_id": link_id})
    if not link or float(link.get("amount") or 0) != float(data.get("link_amount") or 0):
        return False
    await set_sales_action_state(
        org_id=str(link["org_id"]), case_id=str(link["case_id"]),
        source_message_id=f"cashfree:{link_id}", action_type="create_payment_link",
        status="completed", fields={"payment_status": "paid", "paid_at": datetime.now(timezone.utc)},
        stage="won",
    )
    try:
        from services.followup_service import abort_scheduled_message
        await abort_scheduled_message(str(link["org_id"]), str(link.get("conversation_id") or ""))
    except Exception as exc:
        logger.error(f"Could not cancel paid sales follow-up: {exc}")
    return True
