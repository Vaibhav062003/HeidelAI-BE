"""Auditable human-send pricing decisions and per-organization rollout state."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from config.settings import PRICING_FLOOR_MAX_AGE_MINUTES, PRICING_INTELLIGENCE_ORG_IDS
from database import get_async_mongo_db
from services.pricing_policy import PriceSnapshot, QuoteCandidate, decide_pricing_v1
from services.sales_offer_policy import (
    quote_revision_snapshot,
    render_structured_offer_message,
    structured_offer_snapshot,
)


DECISION_TTL = timedelta(minutes=15)
DECISION_RETENTION = timedelta(days=30)
SHADOW_PERIOD = timedelta(days=7)
_IDENTIFIER_FIELDS = (
    "unique_identifier", "model_no", "sku", "sku_full", "product_id",
    "tally_master_id", "tally_guid", "tally_alter_id",
)


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"Unsupported pricing value: {type(value).__name__}")


def _fingerprint(value: dict) -> str:
    payload = json.dumps(value, default=_json_value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decision_collection(org_id: str):
    return get_async_mongo_db()[f"pricing_decisions_{org_id}"]


async def get_pricing_settings(org_id: str) -> dict:
    """Return the effective mode; the environment allowlist is the kill switch."""
    if not org_id or org_id not in PRICING_INTELLIGENCE_ORG_IDS:
        return {"org_id": org_id, "eligible": False, "mode": "off", "shadow_started_at": None}
    collection = get_async_mongo_db().pricing_settings
    stored = await collection.find_one({"_id": org_id})
    if not stored:
        now = datetime.now(timezone.utc)
        await collection.update_one(
            {"_id": org_id},
            {"$setOnInsert": {
                "org_id": org_id,
                "mode": "shadow",
                "shadow_started_at": now,
                "updated_at": now,
                "updated_by": "pricing_rollout",
            }},
            upsert=True,
        )
        stored = await collection.find_one({"_id": org_id}) or {
            "mode": "shadow", "shadow_started_at": now,
            "updated_at": now, "updated_by": "pricing_rollout",
        }
    mode = str((stored or {}).get("mode") or "shadow")
    if mode not in {"off", "shadow", "review"}:
        mode = "shadow"
    return {
        "org_id": org_id,
        "eligible": True,
        "mode": mode,
        "shadow_started_at": (stored or {}).get("shadow_started_at"),
        "updated_at": (stored or {}).get("updated_at"),
        "updated_by": (stored or {}).get("updated_by"),
    }


async def update_pricing_settings(org_id: str, mode: str, actor_id: str) -> dict:
    if mode not in {"off", "shadow", "review"}:
        raise ValueError("Pricing mode must be off, shadow, or review")
    if mode != "off" and org_id not in PRICING_INTELLIGENCE_ORG_IDS:
        raise PermissionError("Organization is not enabled by PRICING_INTELLIGENCE_ORG_IDS")

    now = datetime.now(timezone.utc)
    current = await get_pricing_settings(org_id)
    shadow_started = current.get("shadow_started_at")
    if mode == "shadow" and current["mode"] != "shadow":
        shadow_started = now
    if mode == "off":
        shadow_started = None
    if mode == "review" and current["mode"] != "review":
        if current["mode"] != "shadow" or not shadow_started or now - _utc(shadow_started) < SHADOW_PERIOD:
            raise ValueError("Review mode requires seven completed days in shadow mode")

    update = {
        "org_id": org_id,
        "mode": mode,
        "shadow_started_at": shadow_started,
        "updated_at": now,
        "updated_by": actor_id,
    }
    await get_async_mongo_db().pricing_settings.update_one(
        {"_id": org_id}, {"$set": update}, upsert=True,
    )
    return {**update, "eligible": org_id in PRICING_INTELLIGENCE_ORG_IDS}


async def _catalogue_product(org_id: str, identifier: str) -> dict | None:
    identifier = str(identifier or "").strip()
    if not identifier:
        return None
    db = get_async_mongo_db()
    organization = await db.organizations.find_one(
        {"org_id": org_id}, {"sales_catalogue_org_id": 1},
    ) or {}
    configured_catalogue = str(organization.get("sales_catalogue_org_id") or "")
    catalogue_org_id = (
        configured_catalogue
        if re.fullmatch(r"[A-Za-z0-9_-]+", configured_catalogue)
        else org_id
    )
    matches = await db[f"products_{catalogue_org_id}"].find(
        {"$or": [{field: identifier} for field in _IDENTIFIER_FIELDS]},
    ).limit(2).to_list(length=2)
    return matches[0] if len(matches) == 1 else None


async def _quote_snapshot(org_id: str, conversation_id: str, context: dict) -> tuple[dict | None, list[str]]:
    reference = context.get("quote") if isinstance(context, dict) else None
    if not isinstance(reference, dict):
        return None, []
    quote_id = str(reference.get("id") or "").strip()
    if not quote_id:
        return None, ["missing_quote_id"]
    quote = await get_async_mongo_db()[f"quotes_{org_id}"].find_one({
        "org_id": org_id, "conversation_id": conversation_id, "quote_id": quote_id,
    })
    if not quote:
        return None, ["quote_not_found"]
    requested_revision = reference.get("revision") or quote.get("revision_number") or 1
    snapshot = quote_revision_snapshot(quote, requested_revision)
    if not snapshot:
        return None, ["quote_revision_not_found"]
    return {
        "quote_id": quote_id,
        "revision": int(snapshot.get("revision_number") or requested_revision),
        "items": [{
            **item,
            "unit_price": item.get("private_unit_price"),
        } for item in snapshot.get("items") or []],
        "discount": float(snapshot.get("discount") or 0),
    }, []


def _candidate_identifier(item: dict) -> str:
    return str(
        item.get("sku") or item.get("tally_master_id") or item.get("tally_guid")
        or item.get("tally_alter_id") or ""
    ).strip()


async def _evaluate_items(org_id: str, items: list[dict], now: datetime) -> tuple[list[dict], list[str]]:
    results: list[dict] = []
    warnings: list[str] = []
    for item in items:
        identifier = _candidate_identifier(item)
        if not identifier:
            warnings.append("missing_product_identifier")
            results.append({"sku": "", "reasons": ["missing_product_identifier"]})
            continue
        product = await _catalogue_product(org_id, identifier)
        if not product:
            warnings.append("product_not_found_or_ambiguous")
            results.append({"sku": identifier, "reasons": ["product_not_found_or_ambiguous"]})
            continue
        try:
            snapshot = PriceSnapshot.from_product(product)
            quantity_value = item.get("quantity", 1)
            quantity = int(quantity_value)
            if quantity != quantity_value:
                raise ValueError("quantity must be a whole number")
            candidate = QuoteCandidate(
                amount=item.get("unit_price"),
                sku=snapshot.sku,
                currency=str(item.get("currency") or snapshot.currency),
                gst_inclusive=item.get("gst_inclusive"),
                gst_rate=item.get("gst_rate"),
                quantity=quantity,
                structured=True,
                exact_product_match=True,
                actor="human",
            )
            decision = decide_pricing_v1(
                snapshot,
                candidate,
                now=now,
                max_age=timedelta(minutes=PRICING_FLOOR_MAX_AGE_MINUTES),
            )
            result = json.loads(json.dumps(asdict(decision), default=_json_value))
            result["sku"] = snapshot.sku
            result["quoted_unit_price"] = str(candidate.amount)
            result["quoted_gst_rate"] = str(candidate.gst_rate) if candidate.gst_rate is not None else None
            result["quantity"] = quantity
            results.append(result)
            if decision.action == "warning_required":
                warnings.extend(decision.reasons)
        except (TypeError, ValueError) as exc:
            warnings.append("invalid_pricing_snapshot")
            results.append({"sku": identifier, "reasons": ["invalid_pricing_snapshot"], "detail": str(exc)})
    return results, list(dict.fromkeys(warnings))


async def evaluate_human_pricing(
    *, org_id: str, user_id: str, conversation_id: str, content: str,
    context: dict | None, temp_id: str | None = None, persist: bool = True,
) -> dict:
    settings = await get_pricing_settings(org_id)
    mode = settings["mode"]
    now = datetime.now(timezone.utc)
    raw_context = context if isinstance(context, dict) else {}
    warnings: list[str] = []
    source = "none"
    resolved: dict[str, Any] = {"content": content or ""}
    if temp_id:
        resolved["temp_id"] = temp_id
    items: list[dict] = []

    offer = structured_offer_snapshot(raw_context)
    if offer:
        resolved["content"] = render_structured_offer_message(offer)
        source = "chat_offer"
        items = offer["items"]
        resolved["offer"] = offer
    else:
        quote, quote_warnings = await _quote_snapshot(org_id, conversation_id, raw_context)
        warnings.extend(quote_warnings)
        if quote:
            source = "formal_quote"
            items = quote["items"]
            resolved["quote"] = quote
            if not items:
                warnings.append("quote_has_no_items")
            if quote["discount"] > 0:
                warnings.append("quote_discount_requires_review")
        elif quote_warnings:
            source = "formal_quote"
            resolved["quote"] = raw_context.get("quote")
        else:
            from services.pricing_service import extract_quoted_prices

            prices = extract_quoted_prices(content or "")
            if prices:
                source = "unstructured_message"
                resolved["prices"] = prices
                warnings.append("unstructured_price_requires_review")

    evaluated, item_warnings = await _evaluate_items(org_id, items, now)
    warnings.extend(item_warnings)
    warnings = list(dict.fromkeys(warnings))
    relevant = source != "none"
    decision = {
        "_id": str(uuid.uuid4()),
        "schema_version": "human_pricing_decision.v1",
        "org_id": org_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "temp_id": temp_id,
        "mode": mode,
        "source": source,
        "relevant": relevant,
        "requires_acknowledgement": mode == "review" and bool(warnings),
        "warnings": warnings,
        "items": evaluated,
        "fingerprint": _fingerprint({"request": resolved, "items": evaluated, "warnings": warnings}),
        "price_versions": sorted({str(item.get("price_version")) for item in evaluated if item.get("price_version")}),
        "created_at": now,
        "ack_expires_at": now + DECISION_TTL,
        "expires_at": now + DECISION_RETENTION,
    }
    if persist and mode != "off" and relevant:
        await _decision_collection(org_id).insert_one(decision)
    return decision


async def require_human_pricing_acknowledgement(
    *, org_id: str, user_id: str, conversation_id: str, content: str,
    context: dict | None, temp_id: str, pricing_decision_id: str | None,
) -> dict | None:
    current = await evaluate_human_pricing(
        org_id=org_id,
        user_id=user_id,
        conversation_id=conversation_id,
        content=content,
        context=context,
        temp_id=temp_id,
        persist=False,
    )
    if current["mode"] == "off" or not current["relevant"]:
        return None
    if current["mode"] == "shadow" or not current["warnings"]:
        await _decision_collection(org_id).insert_one(current)
        return current

    stored = None
    if pricing_decision_id:
        stored = await _decision_collection(org_id).find_one({
            "_id": pricing_decision_id,
            "org_id": org_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "temp_id": temp_id,
            "mode": "review",
            "requires_acknowledgement": True,
            "ack_expires_at": {"$gt": datetime.now(timezone.utc)},
            "sent_at": {"$exists": False},
            "sending_at": {"$exists": False},
        })
    if stored and all(stored.get(key) == current.get(key) for key in ("fingerprint", "price_versions", "warnings")):
        claimed = await _decision_collection(org_id).update_one(
            {
                "_id": stored["_id"],
                "sent_at": {"$exists": False},
                "sending_at": {"$exists": False},
            },
            {"$set": {
                "acknowledged_at": datetime.now(timezone.utc),
                "sending_at": datetime.now(timezone.utc),
            }},
        )
        if claimed.matched_count:
            return {**current, "_id": stored["_id"], "acknowledged": True}

    await _decision_collection(org_id).insert_one(current)
    return current


async def mark_pricing_decision_sent(org_id: str, decision_id: str, message_id: str | None) -> None:
    if not decision_id:
        return
    await _decision_collection(org_id).update_one(
        {"_id": decision_id, "sent_at": {"$exists": False}},
        {
            "$set": {"sent_at": datetime.now(timezone.utc), "provider_message_id": message_id},
            "$unset": {"sending_at": ""},
        },
    )


async def release_pricing_decision_claim(org_id: str, decision_id: str) -> None:
    if not decision_id:
        return
    await _decision_collection(org_id).update_one(
        {"_id": decision_id, "sent_at": {"$exists": False}},
        {"$unset": {"sending_at": "", "acknowledged_at": ""}},
    )
