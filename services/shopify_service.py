"""Shopify commerce import and single-shot abandoned-checkout recovery."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from loguru import logger
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from config.settings import SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_PUBLIC_BASE_URL, WA_VERSION
from database import get_async_mongo_db, get_mongo_db
from schemas.models import MessageRole
from services.wa_service import create_or_update_whatsapp_conversation, store_whatsapp_message
from services.websocket_service import broadcast_org_admin_ws, broadcast_org_ws


API_VERSION = "2025-10"
OAUTH_SCOPES = "read_orders,read_customers"
ALLOWED_DELAYS = {15, 30, 60, 120, 240, 1440}
SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
ACTIVE_REMINDERS = {"pending", "retryable", "sending", "provider_accepted"}
PROVIDER_STARTED_REMINDERS = {"sending", "provider_accepted", "delivery_unknown", "sent"}
RECONCILABLE_REMINDERS = {
    "pending", "retryable", "historical", "permission_missing", "phone_missing",
    "configuration_failure", "cancelled", "superseded", "opted_out",
}
TERMINAL_REMINDERS = {
    "sent", "converted", "cancelled", "delivery_unknown", "permission_missing",
    "phone_missing", "configuration_failure", "failed", "historical", "superseded", "opted_out",
}
OPT_OUT_COMMANDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
OPT_IN_COMMANDS = {"start", "subscribe"}
OAUTH_TTL_MINUTES = 10
WEBHOOK_TTL_DAYS = 30
CLAIM_LEASE_MINUTES = 5

db = get_async_mongo_db()
sync_db = get_mongo_db()


CHECKOUTS_QUERY = """
query ShopifyCheckouts($first: Int!, $after: String, $query: String!, $sortKey: AbandonedCheckoutSortKeys!) {
  abandonedCheckouts(first: $first, after: $after, query: $query, sortKey: $sortKey) {
    nodes {
      id abandonedCheckoutUrl createdAt updatedAt completedAt
      customer {
        id firstName lastName
        defaultPhoneNumber { phoneNumber }
      }
      shippingAddress { phone }
      billingAddress { phone }
      lineItems(first: 50) {
        nodes { id title quantity originalTotalPriceSet { shopMoney { amount currencyCode } } }
      }
      totalPriceSet { shopMoney { amount currencyCode } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

CHECKOUT_NODE_QUERY = """
query ShopifyCheckout($id: ID!) {
  node(id: $id) {
    ... on AbandonedCheckout {
      id abandonedCheckoutUrl createdAt updatedAt completedAt
      customer {
        id firstName lastName
        defaultPhoneNumber { phoneNumber }
      }
      shippingAddress { phone }
      billingAddress { phone }
      lineItems(first: 50) {
        nodes { id title quantity originalTotalPriceSet { shopMoney { amount currencyCode } } }
      }
      totalPriceSet { shopMoney { amount currencyCode } }
    }
  }
}
"""

ORDERS_QUERY = """
query ShopifyOrders($first: Int!, $after: String, $query: String!, $sortKey: OrderSortKeys!) {
  orders(first: $first, after: $after, query: $query, sortKey: $sortKey) {
    nodes {
      id name createdAt updatedAt cancelledAt
      displayFinancialStatus displayFulfillmentStatus
      customer {
        id firstName lastName
        defaultPhoneNumber { phoneNumber }
      }
      shippingAddress { phone }
      billingAddress { phone }
      lineItems(first: 50) {
        nodes { id name quantity originalTotalSet { shopMoney { amount currencyCode } } }
      }
      totalPriceSet { shopMoney { amount currencyCode } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

WEBHOOK_CREATE_MUTATION = """
mutation ShopifyWebhook($topic: WebhookSubscriptionTopic!, $uri: String!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: { uri: $uri, format: JSON }) {
    webhookSubscription { id topic }
    userErrors { field message }
  }
}
"""

WEBHOOK_TOPICS = (
    "CHECKOUTS_CREATE", "CHECKOUTS_UPDATE", "CHECKOUTS_DELETE",
    "ORDERS_CREATE", "ORDERS_UPDATED", "ORDERS_CANCELLED", "ORDERS_DELETE",
    "CUSTOMERS_UPDATE", "APP_UNINSTALLED",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _provider_time(document: dict) -> datetime:
    return (
        _parse_datetime(document.get("provider_updated_at"))
        or _parse_datetime(document.get("updated_at"))
        or _parse_datetime(document.get("provider_created_at"))
        or _parse_datetime(document.get("created_at"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )


def _provider_sort_key(document: dict) -> tuple[datetime, datetime, str]:
    return (
        _provider_time(document),
        _parse_datetime(document.get("provider_created_at") or document.get("created_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        str(document.get("provider_id") or ""),
    )


def _reminder_due(provider_updated_at: Any, delay_minutes: int, frequency_after: Any = None) -> datetime:
    due = (_parse_datetime(provider_updated_at) or utcnow()) + timedelta(minutes=delay_minutes)
    frequency_due = _parse_datetime(frequency_after)
    return max(due, frequency_due) if frequency_due else due


def _recovery_activity_time(provider_updated_at: Any, automation_enabled_at: Any) -> datetime | None:
    """Treat a recently active cart as new when recovery is first enabled."""
    updated = _parse_datetime(provider_updated_at)
    enabled = _parse_datetime(automation_enabled_at)
    if not updated or not enabled:
        return None
    if updated >= enabled:
        return updated
    return enabled if updated >= enabled - timedelta(hours=24) else None


def _checkout_changed_for_send(expected: dict, current: dict | None) -> bool:
    if not current:
        return True
    return any(current.get(key) != expected.get(key) for key in (
        "provider_updated_at", "phone", "products", "recovery_url", "completed_at", "record_status",
    ))


def _sync_retry_at(failures: int, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(minutes=min(2 ** max(failures - 1, 0), 30))


def normalize_shop(value: str) -> str:
    shop = (value or "").strip().lower()
    shop = re.sub(r"^https?://", "", shop).split("/", 1)[0].rstrip(".")
    if not SHOP_RE.fullmatch(shop):
        raise HTTPException(status_code=422, detail="Enter a valid *.myshopify.com store domain")
    return shop


def normalize_phone(value: Any) -> str:
    candidate = re.sub(r"[^\d+]", "", str(value or "").strip())
    return candidate if PHONE_RE.fullmatch(candidate) else ""


def _phone_key(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if 8 <= len(digits) <= 15 else ""


def _message_preference(content: Any) -> str | None:
    command = re.sub(r"[^a-z]", "", str(content or "").casefold())
    if command in OPT_OUT_COMMANDS:
        return "opted_out"
    if command in OPT_IN_COMMANDS:
        return "subscribed"
    return None


def _provider_reports_block(payload: dict) -> bool:
    errors = json.dumps(payload.get("errors") or payload.get("error") or [], ensure_ascii=True)
    return bool(re.search(r"\b(?:recipient|user|customer)\b.{0,40}\bblocked\b|\bblocked\b.{0,40}\b(?:recipient|user|customer)\b", errors, re.IGNORECASE))


def _cancel_suppressed_shopify_reminders(org_id: str, phone_key: str, now: datetime) -> None:
    sync_db.shopify_checkouts.update_many(
        {"org_id": org_id, "phone": {"$in": [phone_key, f"+{phone_key}"]}, "$or": [
            {"reminder_status": {"$in": ["pending", "retryable"]}},
            {"reminder_status": "sending", "provider_started_at": {"$exists": False}},
        ]},
        {"$set": {"reminder_status": "opted_out", "failure_category": "customer_opt_out", "reminder_due_at": None, "status_updated_at": now}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )


def record_whatsapp_preference(org_id: str, phone: Any, content: Any) -> None:
    preference = _message_preference(content)
    phone_key = _phone_key(phone)
    if not preference or not phone_key:
        return
    if preference == "subscribed":
        sync_db.whatsapp_suppressions.delete_one({"org_id": org_id, "phone": phone_key})
        sync_db[f"contacts_{org_id}"].update_many(
            {"$or": [{"phone_number": {"$in": [phone_key, f"+{phone_key}"]}}, {"conversation_id": f"whatsapp_{phone_key}"}]},
            {"$set": {"marketing_opted_out": False, "marketing_preference_updated_at": utcnow()}},
        )
        return
    now = utcnow()
    sync_db.whatsapp_suppressions.update_one(
        {"org_id": org_id, "phone": phone_key},
        {"$set": {"reason": "customer_opt_out", "source": "inbound_message", "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    sync_db[f"contacts_{org_id}"].update_many(
        {"$or": [{"phone_number": {"$in": [phone_key, f"+{phone_key}"]}}, {"conversation_id": f"whatsapp_{phone_key}"}]},
        {"$set": {"marketing_opted_out": True, "marketing_preference_updated_at": now}},
    )
    _cancel_suppressed_shopify_reminders(org_id, phone_key, now)


def record_whatsapp_provider_block(org_id: str, phone: Any, payload: dict) -> None:
    if not _provider_reports_block(payload):
        return
    phone_key = _phone_key(phone)
    if not phone_key:
        return
    now = utcnow()
    sync_db.whatsapp_suppressions.update_one(
        {"org_id": org_id, "phone": phone_key},
        {"$set": {"reason": "provider_block", "source": "delivery_status", "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    sync_db[f"contacts_{org_id}"].update_many(
        {"$or": [{"phone_number": {"$in": [phone_key, f"+{phone_key}"]}}, {"conversation_id": f"whatsapp_{phone_key}"}]},
        {"$set": {"marketing_opted_out": True, "marketing_preference_updated_at": now}},
    )
    _cancel_suppressed_shopify_reminders(org_id, phone_key, now)


def _money(value: Any) -> dict:
    money = ((value or {}).get("shopMoney") or (value or {}).get("shop_money") or {})
    try:
        amount = float(money.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    return {"amount": amount, "currency": money.get("currencyCode") or money.get("currency_code") or ""}


def _name(customer: dict | None) -> str:
    customer = customer or {}
    return " ".join(part for part in (customer.get("firstName") or customer.get("first_name"), customer.get("lastName") or customer.get("last_name")) if part).strip()


def _phone_and_consent(customer: dict | None) -> tuple[str, str, datetime | None]:
    customer = customer or {}
    phone = customer.get("defaultPhoneNumber") or customer.get("default_phone_number") or {}
    if isinstance(phone, str):
        return normalize_phone(phone), "UNKNOWN", None
    consent = (
        phone.get("whatsAppMarketingConsent") or phone.get("whatsapp_marketing_consent")
        or customer.get("whats_app_marketing_consent") or {}
    )
    if not consent and str(customer.get("marketing_channel") or customer.get("channel") or "").upper() in {"WHATSAPP", "WHATS_APP"}:
        consent = customer.get("marketing_consent") or customer.get("consent") or {}
    return (
        normalize_phone(phone.get("phoneNumber") or phone.get("phone_number") or customer.get("phone_number") or customer.get("phone")),
        str(consent.get("state") or "UNKNOWN").upper(),
        _parse_datetime(consent.get("updatedAt") or consent.get("updated_at") or consent.get("consent_updated_at")),
    )


def _record_phone_and_consent(
    customer: dict | None,
    shipping_address: dict | None,
    billing_address: dict | None,
    direct_phone: Any = None,
) -> tuple[str, str, datetime | None]:
    phone, consent, consent_at = _phone_and_consent(customer)
    return (
        phone
        or normalize_phone((shipping_address or {}).get("phone"))
        or normalize_phone((billing_address or {}).get("phone"))
        or normalize_phone(direct_phone),
        consent,
        consent_at,
    )


def _fernet() -> Fernet:
    if not SHOPIFY_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Shopify integration is not configured")
    key = base64.urlsafe_b64encode(hashlib.sha256(SHOPIFY_CLIENT_SECRET.encode()).digest())
    return Fernet(key)


def _seal(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _unseal(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, AttributeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Reconnect Shopify to refresh authorization") from exc


def _safe_document(document: dict | None, *, include_url: bool = False) -> dict | None:
    if not document:
        return None
    internal = {
        "_id", "access_token", "refresh_token", "claim_token", "claim_expires_at", "claimed_at",
        "customer_id", "checkout_token", "cart_token", "provider_message_id", "provider_started_at", "provider_accepted_at",
        "consent_updated_at", "attempts", "pre_provider_attempts", "shop", "send_whatsapp_id", "send_phone_id",
        "send_template_name", "send_template_language",
    }
    value = {key: item for key, item in document.items() if key not in internal}
    for key, item in list(value.items()):
        if isinstance(item, datetime):
            value[key] = _iso_utc(item)
    if not include_url:
        value.pop("recovery_url", None)
    return value


async def ensure_shopify_indexes() -> None:
    await db.shopify_oauth_states.create_index("expires_at", expireAfterSeconds=0)
    await db.shopify_connections.create_index("org_id", unique=True)
    await db.shopify_connections.create_index("shop", unique=True, sparse=True)
    await db.shopify_checkouts.create_index([("org_id", 1), ("provider_id", 1)], unique=True)
    await db.shopify_checkouts.create_index([("org_id", 1), ("phone", 1), ("updated_at", -1)])
    await db.shopify_checkouts.create_index([("org_id", 1), ("phone", 1), ("provider_updated_at", -1), ("provider_id", -1)])
    await db.shopify_checkouts.create_index([("org_id", 1), ("checkout_token", 1)])
    await db.shopify_checkouts.create_index([("org_id", 1), ("updated_at", -1), ("provider_id", -1)])
    await db.shopify_checkouts.create_index([("org_id", 1), ("reminder_status", 1), ("updated_at", -1)])
    await db.shopify_checkouts.create_index([("shop", 1), ("provider_id", 1)])
    await db.shopify_checkouts.create_index([("reminder_status", 1), ("reminder_due_at", 1)])
    await db.shopify_orders.create_index([("org_id", 1), ("provider_id", 1)], unique=True)
    await db.shopify_orders.create_index([("org_id", 1), ("phone", 1), ("created_at", -1)])
    await db.shopify_orders.create_index([("org_id", 1), ("provider_updated_at", -1), ("provider_id", -1)])
    await db.shopify_orders.create_index([("org_id", 1), ("updated_at", -1), ("provider_id", -1)])
    await db.shopify_orders.create_index([("org_id", 1), ("payment_status", 1), ("updated_at", -1)])
    await db.shopify_orders.create_index([("shop", 1), ("provider_id", 1)])
    await db.shopify_webhook_receipts.create_index("received_at", expireAfterSeconds=WEBHOOK_TTL_DAYS * 86400)
    await db.shopify_webhook_receipts.create_index([("shop", 1), ("webhook_id", 1)], unique=True)
    await db.whatsapp_suppressions.create_index([("org_id", 1), ("phone", 1)], unique=True)


def _require_configuration() -> None:
    if not all((SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_PUBLIC_BASE_URL)):
        raise HTTPException(status_code=503, detail="Shopify integration is not configured")
    if not SHOPIFY_PUBLIC_BASE_URL.startswith("https://"):
        raise HTTPException(status_code=503, detail="Shopify public URL must use HTTPS")


async def create_oauth_url(org_id: str, user_id: str, shop_value: str) -> str:
    _require_configuration()
    shop = normalize_shop(shop_value)
    current = await db.shopify_connections.find_one({"org_id": org_id, "status": "connected"}, {"shop": 1})
    if current and current.get("shop") != shop:
        raise HTTPException(status_code=409, detail="Disconnect the current Shopify store before connecting another")
    token = secrets.token_urlsafe(32)
    state_id = hashlib.sha256(token.encode()).hexdigest()
    now = utcnow()
    await db.shopify_oauth_states.insert_one({
        "_id": state_id,
        "org_id": org_id,
        "user_id": user_id,
        "shop": shop,
        "created_at": now,
        "expires_at": now + timedelta(minutes=OAUTH_TTL_MINUTES),
    })
    callback = f"{SHOPIFY_PUBLIC_BASE_URL}/api/integrations/shopify/oauth/callback"
    return f"https://{shop}/admin/oauth/authorize?{urlencode({'client_id': SHOPIFY_CLIENT_ID, 'scope': OAUTH_SCOPES, 'redirect_uri': callback, 'state': token})}"


def verify_callback_hmac(params: dict[str, str]) -> None:
    supplied = params.get("hmac", "")
    canonical = "&".join(f"{key}={value}" for key, value in sorted(params.items()) if key not in {"hmac", "signature"})
    expected = hmac.new(SHOPIFY_CLIENT_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid Shopify callback signature")


async def _exchange_token(shop: str, code: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://{shop}/admin/oauth/access_token",
                json={"client_id": SHOPIFY_CLIENT_ID, "client_secret": SHOPIFY_CLIENT_SECRET, "code": code},
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Shopify token exchange failed: {}", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Shopify authorization could not be completed") from exc
    if not payload.get("access_token"):
        raise HTTPException(status_code=502, detail="Shopify did not return an access token")
    return payload


async def complete_oauth(params: dict[str, str]) -> str:
    _require_configuration()
    verify_callback_hmac(params)
    shop = normalize_shop(params.get("shop", ""))
    code = (params.get("code") or "").strip()
    state = params.get("state") or ""
    if not code or not state:
        raise HTTPException(status_code=422, detail="Incomplete Shopify callback")
    state_doc = await db.shopify_oauth_states.find_one_and_delete({
        "_id": hashlib.sha256(state.encode()).hexdigest(),
        "shop": shop,
        "expires_at": {"$gt": utcnow()},
    })
    if not state_doc:
        raise HTTPException(status_code=409, detail="Shopify authorization has expired or was already used")
    token = await _exchange_token(shop, code)
    now = utcnow()
    existing = await db.shopify_connections.find_one({"org_id": state_doc["org_id"]}, {"shop": 1, "status": 1})
    if existing and existing.get("status") == "connected" and existing.get("shop") != shop:
        raise HTTPException(status_code=409, detail="Disconnect the current Shopify store before connecting another")
    owner = await db.shopify_connections.find_one({"shop": shop, "org_id": {"$ne": state_doc["org_id"]}, "status": {"$ne": "disconnected"}}, {"_id": 1})
    if owner:
        raise HTTPException(status_code=409, detail="This Shopify store is already connected to another organization")
    update = {
        "org_id": state_doc["org_id"], "shop": shop, "status": "connected",
        "access_token": _seal(token["access_token"]),
        "scopes": sorted(set(str(token.get("scope") or OAUTH_SCOPES).split(","))),
        "connected_by": state_doc["user_id"], "connected_at": now, "updated_at": now,
        "sync_status": "pending", "sync_requested_at": now, "sync_full": True,
        "sync_error": None, "webhook_error": None, "reminder_error": None, "last_error": None,
    }
    if not existing or existing.get("status") == "disconnected":
        update.update({
            "automation_enabled": False, "reminder_delay_minutes": 30,
            "whatsapp_id": None, "template_name": None, "template_status": None,
            "sync_cursor": {}, "import_counts": {"checkouts": 0, "orders": 0},
        })
    if token.get("refresh_token"):
        update["refresh_token"] = _seal(token["refresh_token"])
    if token.get("expires_in"):
        update["token_expires_at"] = now + timedelta(seconds=int(token["expires_in"]))
    try:
        await db.shopify_connections.update_one({"org_id": state_doc["org_id"]}, {"$set": update}, upsert=True)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="This Shopify store is already connected") from exc
    connection = await db.shopify_connections.find_one({"org_id": state_doc["org_id"]})
    await _register_webhooks(connection)
    await _commerce_event(state_doc["org_id"], "connection", state_doc["org_id"])
    return state_doc["org_id"]


async def _access_token(connection: dict) -> str:
    token = _unseal(connection.get("access_token", ""))
    expiry = _parse_datetime(connection.get("token_expires_at"))
    if not expiry or expiry > utcnow() + timedelta(minutes=2):
        return token
    refresh = connection.get("refresh_token")
    if not refresh:
        raise HTTPException(status_code=503, detail="Reconnect Shopify to refresh authorization")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://{connection['shop']}/admin/oauth/access_token",
                json={
                    "client_id": SHOPIFY_CLIENT_ID, "client_secret": SHOPIFY_CLIENT_SECRET,
                    "grant_type": "refresh_token", "refresh_token": _unseal(refresh),
                },
            )
        response.raise_for_status()
        payload = response.json()
        update = {"access_token": _seal(payload["access_token"]), "updated_at": utcnow()}
        if payload.get("refresh_token"):
            update["refresh_token"] = _seal(payload["refresh_token"])
        if payload.get("expires_in"):
            update["token_expires_at"] = utcnow() + timedelta(seconds=int(payload["expires_in"]))
        await db.shopify_connections.update_one({"org_id": connection["org_id"], "shop": connection["shop"]}, {"$set": update})
        return payload["access_token"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Shopify token refresh failed: {}", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Reconnect Shopify to refresh authorization") from exc


async def _graphql(connection: dict, query: str, variables: dict, *, timeout: float = 20) -> dict:
    headers = {"X-Shopify-Access-Token": await _access_token(connection), "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"https://{connection['shop']}/admin/api/{API_VERSION}/graphql.json",
                    headers=headers, json={"query": query, "variables": variables},
                )
            if response.status_code == 429:
                retry = min(int(response.headers.get("Retry-After", "2") or 2), 30)
                if attempt < 2:
                    await asyncio.sleep(max(retry, 1))
                    continue
                raise HTTPException(status_code=503, detail="Shopify is busy; synchronization will resume")
            response.raise_for_status()
            payload = response.json()
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Shopify GraphQL request failed: {}", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Shopify is temporarily unavailable") from exc
        errors = payload.get("errors") or []
        throttled = errors and all(str((item.get("extensions") or {}).get("code") or "").upper() == "THROTTLED" for item in errors)
        if throttled and attempt < 2:
            cost = (payload.get("extensions") or {}).get("cost") or {}
            throttle = cost.get("throttleStatus") or {}
            requested = float(cost.get("requestedQueryCost") or cost.get("actualQueryCost") or 1)
            available = float(throttle.get("currentlyAvailable") or 0)
            restore = max(float(throttle.get("restoreRate") or 1), 1)
            await asyncio.sleep(min(max((requested - available) / restore, 1), 30))
            continue
        if errors:
            logger.warning("Shopify GraphQL returned {} error(s)", len(errors))
            raise HTTPException(status_code=503 if throttled else 502, detail="Shopify could not complete the request")
        return payload.get("data") or {}
    raise HTTPException(status_code=503, detail="Shopify is busy; synchronization will resume")


async def _register_webhooks(connection: dict) -> None:
    callback = f"{SHOPIFY_PUBLIC_BASE_URL}/api/webhooks/shopify"
    semaphore = asyncio.Semaphore(3)

    async def register(topic: str) -> int:
        async with semaphore:
            try:
                data = await _graphql(connection, WEBHOOK_CREATE_MUTATION, {"topic": topic, "uri": callback})
                errors = (data.get("webhookSubscriptionCreate") or {}).get("userErrors") or []
                return int(any("already" not in str(error.get("message") or "").lower() for error in errors))
            except HTTPException:
                return 1

    failures = sum(await asyncio.gather(*(register(topic) for topic in WEBHOOK_TOPICS)))
    if failures:
        await db.shopify_connections.update_one({"org_id": connection["org_id"]}, {"$set": {"webhook_error": "Some Shopify webhooks require attention", "updated_at": utcnow()}})
        await _admin_warning(connection["org_id"], "webhook_configuration")
    else:
        await db.shopify_connections.update_one({"org_id": connection["org_id"]}, {"$set": {"webhook_error": None, "updated_at": utcnow()}})


def _checkout_from_graphql(org_id: str, shop: str, node: dict, historical: bool, delay_minutes: int = 30) -> dict:
    customer = node.get("customer") or {}
    phone, consent, consent_at = _record_phone_and_consent(
        customer, node.get("shippingAddress"), node.get("billingAddress"),
    )
    items = []
    for item in ((node.get("lineItems") or {}).get("nodes") or []):
        items.append({"id": item.get("id"), "name": item.get("title") or "Product", "quantity": int(item.get("quantity") or 0), "total": _money(item.get("originalTotalPriceSet"))})
    updated = _parse_datetime(node.get("updatedAt")) or utcnow()
    created = _parse_datetime(node.get("createdAt")) or updated
    recovery_url = str(node.get("abandonedCheckoutUrl") or "")
    token_match = re.search(r"/([^/?]+)/recover(?:[/?]|$)", recovery_url)
    return {
        "org_id": org_id, "shop": shop, "provider_id": str(node.get("id")),
        "checkout_token": token_match.group(1) if token_match else "",
        "customer_id": customer.get("id"), "customer_name": _name(customer), "phone": phone,
        "whatsapp_consent": consent, "consent_updated_at": consent_at,
        "products": items, "total": _money(node.get("totalPriceSet")),
        "recovery_url": recovery_url,
        "created_at": created, "provider_created_at": created,
        "updated_at": updated, "provider_updated_at": updated, "completed_at": _parse_datetime(node.get("completedAt")),
        "record_status": "converted" if node.get("completedAt") else "abandoned",
        "reminder_status": "converted" if node.get("completedAt") else ("historical" if historical else _eligibility_status(phone, items)),
        "reminder_due_at": None if historical or node.get("completedAt") else _reminder_due(updated, delay_minutes),
        "status_updated_at": utcnow(),
    }


def _order_from_graphql(org_id: str, shop: str, node: dict) -> dict:
    customer = node.get("customer") or {}
    phone, _, _ = _record_phone_and_consent(
        customer, node.get("shippingAddress"), node.get("billingAddress"),
    )
    items = []
    for item in ((node.get("lineItems") or {}).get("nodes") or []):
        items.append({"id": item.get("id"), "name": item.get("name") or "Product", "quantity": int(item.get("quantity") or 0), "total": _money(item.get("originalTotalSet"))})
    provider_id = str(node.get("id"))
    created = _parse_datetime(node.get("createdAt")) or utcnow()
    updated = _parse_datetime(node.get("updatedAt")) or created
    return {
        "org_id": org_id, "shop": shop, "provider_id": provider_id, "order_number": node.get("name") or "",
        "order_url": f"https://{shop}/admin/orders/{provider_id.rsplit('/', 1)[-1]}",
        "customer_id": customer.get("id"), "customer_name": _name(customer), "phone": phone,
        "products": items, "total": _money(node.get("totalPriceSet")),
        "payment_status": node.get("displayFinancialStatus") or "", "fulfillment_status": node.get("displayFulfillmentStatus") or "",
        "cancelled_at": _parse_datetime(node.get("cancelledAt")), "record_status": "active",
        "created_at": created, "provider_created_at": created,
        "updated_at": updated, "provider_updated_at": updated, "status_updated_at": utcnow(),
    }


def _eligibility_status(phone: str, items: list) -> str:
    if not items:
        return "cancelled"
    if not phone:
        return "phone_missing"
    return "pending"


async def request_sync(org_id: str) -> None:
    connection = await db.shopify_connections.find_one({"org_id": org_id, "status": "connected"})
    if not connection:
        raise HTTPException(status_code=404, detail="Connect Shopify before synchronizing")
    if connection.get("sync_status") == "syncing" and (_parse_datetime(connection.get("sync_claimed_at")) or utcnow()) > utcnow() - timedelta(minutes=CLAIM_LEASE_MINUTES):
        await db.shopify_connections.update_one(
            {"org_id": org_id, "status": "connected", "sync_status": "syncing"},
            {"$set": {"resync_requested": True, "sync_error": None, "updated_at": utcnow()}},
        )
        return
    result = await db.shopify_connections.update_one(
        {"org_id": org_id, "status": "connected", "$or": [{"sync_status": {"$ne": "syncing"}}, {"sync_claimed_at": {"$lte": utcnow() - timedelta(minutes=CLAIM_LEASE_MINUTES)}}]},
        {"$set": {"sync_status": "pending", "sync_requested_at": utcnow(), "sync_full": True, "sync_cursor": {}, "sync_completed_resources": [], "import_counts": {"checkouts": 0, "orders": 0}, "sync_error": None, "sync_failures": 0, "updated_at": utcnow()}, "$unset": {"sync_cutoff_at": "", "sync_filter_field": "", "next_sync_attempt_at": ""}},
    )
    if not result.matched_count:
        queued = await db.shopify_connections.update_one(
            {"org_id": org_id, "status": "connected", "sync_status": "syncing"},
            {"$set": {"resync_requested": True, "sync_error": None, "updated_at": utcnow()}},
        )
        if not queued.matched_count:
            raise HTTPException(status_code=404, detail="Connect Shopify before synchronizing")
    await _register_webhooks(connection)
    await _commerce_event(org_id, "sync", org_id)


async def _upsert_newer(collection, key: dict, fields: dict, *, on_insert: dict | None = None) -> bool:
    provider_time = fields.get("provider_updated_at") or utcnow()
    result = await collection.update_one(
        {**key, "$or": [{"provider_updated_at": {"$lt": provider_time}}, {"provider_updated_at": {"$exists": False}}]},
        {"$set": fields},
    )
    if result.matched_count:
        return True
    if await collection.find_one(key, {"_id": 1}):
        return False
    try:
        await collection.insert_one({**key, **fields, **(on_insert or {})})
        return True
    except DuplicateKeyError:
        return False


def _delivery_started(checkout: dict) -> bool:
    return bool(checkout.get("provider_started_at") or checkout.get("reminder_status") in PROVIDER_STARTED_REMINDERS)


async def _configuration_ready(connection: dict) -> bool:
    if not (
        connection.get("status") == "connected"
        and connection.get("automation_enabled")
        and connection.get("whatsapp_id")
        and connection.get("template_name")
        and connection.get("template_status") == "APPROVED"
    ):
        return False
    return bool(await _selected_whatsapp_connection(connection, {"_id": 1}))


async def _selected_whatsapp_connection(connection: dict, projection: dict | None = None) -> dict | None:
    return await _whatsapp_connection_for_selector(
        connection["org_id"],
        phone_id=connection.get("whatsapp_phone_id"),
        wa_id=connection.get("whatsapp_id"),
        projection=projection,
        require_automation=True,
    )


async def _whatsapp_connection_for_selector(
    org_id: str,
    *,
    phone_id: str | None = None,
    wa_id: str | None = None,
    projection: dict | None = None,
    require_automation: bool = False,
) -> dict | None:
    query: dict[str, Any] = {"org_id": org_id, "is_active": {"$ne": False}}
    if require_automation:
        query["automation_enabled"] = {"$ne": False}
    if phone_id:
        query["phone_id"] = phone_id
        return await db.whatsapp_connections.find_one(query, projection)
    if wa_id:
        query["wa_id"] = wa_id
    matches = await db.whatsapp_connections.find(query, projection).limit(2).to_list(length=2)
    return matches[0] if len(matches) == 1 else None


async def _reconcile_phone_checkouts(org_id: str, phone: str, connection: dict, *, configured: bool | None = None) -> None:
    if not phone:
        return
    latest = await db.shopify_checkouts.find_one(
        {"org_id": org_id, "phone": phone},
        sort=[("provider_updated_at", -1), ("provider_created_at", -1), ("provider_id", -1)],
    )
    if not latest:
        return
    now = utcnow()
    await db.shopify_checkouts.update_many(
        {
            "org_id": org_id, "phone": phone, "_id": {"$ne": latest["_id"]},
            "reminder_status": {"$in": list(RECONCILABLE_REMINDERS)},
            "provider_started_at": {"$exists": False},
        },
        {"$set": {"reminder_status": "superseded", "failure_category": "newer_checkout", "reminder_due_at": None, "status_updated_at": now}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )
    if _delivery_started(latest) or latest.get("reminder_status") not in RECONCILABLE_REMINDERS:
        return
    status = "pending"
    reason = None
    activation = _parse_datetime(connection.get("automation_enabled_at"))
    if latest.get("record_status") == "converted" or latest.get("completed_at"):
        status, reason = "converted", "order_created"
    elif latest.get("record_status") == "deleted" or not latest.get("products"):
        status, reason = "cancelled", "checkout_unavailable"
    elif not normalize_phone(latest.get("phone")):
        status, reason = "phone_missing", "phone_missing"
    elif await _is_whatsapp_suppressed(org_id, phone):
        status, reason = "opted_out", "customer_opt_out"
    elif not connection.get("automation_enabled"):
        status, reason = "cancelled", "automation_disabled"
    activity_time = _recovery_activity_time(_provider_time(latest), activation)
    if status == "pending" and not activity_time:
        status, reason = "historical", "before_automation_activation"
    elif status == "pending" and not (configured if configured is not None else await _configuration_ready(connection)):
        status, reason = "configuration_failure", "whatsapp_or_template_unavailable"
    due = None
    if status == "pending":
        prior = await db.shopify_checkouts.find_one(
            {"org_id": org_id, "phone": phone, "_id": {"$ne": latest["_id"]}, "reminder_status": {"$in": ["sent", "delivery_unknown"]}, "provider_started_at": {"$exists": True}},
            {"provider_started_at": 1}, sort=[("provider_started_at", -1)],
        )
        frequency_after = (_parse_datetime((prior or {}).get("provider_started_at")) + timedelta(hours=24)) if prior and _parse_datetime(prior.get("provider_started_at")) else None
        due = _reminder_due(activity_time, int(connection.get("reminder_delay_minutes") or 30), frequency_after)
        if frequency_after and frequency_after > now:
            reason = "phone_frequency_cap"
    update = {"reminder_status": status, "reminder_due_at": due, "status_updated_at": now}
    if reason:
        update["failure_category"] = reason
        unset = {"claim_token": "", "claim_expires_at": ""}
    else:
        unset = {"claim_token": "", "claim_expires_at": "", "failure_category": ""}
    await db.shopify_checkouts.update_one(
        {"_id": latest["_id"], "provider_started_at": {"$exists": False}, "reminder_status": {"$in": list(RECONCILABLE_REMINDERS)}},
        {"$set": update, "$unset": unset},
    )


async def _repair_org_checkout_states(org_id: str, connection: dict | None = None, phones: set[str] | None = None) -> None:
    connection = connection or await db.shopify_connections.find_one({"org_id": org_id})
    if not connection:
        return
    configured = await _configuration_ready(connection)
    phone_values = phones if phones is not None else set(await db.shopify_checkouts.distinct("phone", {"org_id": org_id, "phone": {"$ne": ""}}))
    for phone in phone_values:
        await _reconcile_phone_checkouts(org_id, phone, connection, configured=configured)
    await db.shopify_checkouts.update_many(
        {"org_id": org_id, "phone": "", "record_status": "abandoned", "reminder_status": {"$in": list(RECONCILABLE_REMINDERS)}, "provider_started_at": {"$exists": False}},
        {"$set": {"reminder_status": "phone_missing", "failure_category": "phone_missing", "reminder_due_at": None, "status_updated_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )


async def _mark_order_conversion(org_id: str, order: dict) -> set[str]:
    tokens = [value for value in {order.get("cart_token"), order.get("checkout_token")} if value]
    if not tokens:
        return set()
    query = {"org_id": org_id, "checkout_token": {"$in": tokens}}
    affected = set(await db.shopify_checkouts.distinct("phone", query))
    now = utcnow()
    await db.shopify_checkouts.update_many(
        {**query, "$or": [
            {"reminder_status": {"$in": ["pending", "retryable"]}},
            {"reminder_status": "sending", "provider_started_at": {"$exists": False}},
        ]},
        {"$set": {"record_status": "converted", "reminder_status": "converted", "failure_category": "order_created", "converted_order_id": order["provider_id"], "reminder_due_at": None, "status_updated_at": now}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )
    await db.shopify_checkouts.update_many(
        {**query, "reminder_status": {"$in": ["sending", "provider_accepted", "delivery_unknown", "sent"]}},
        {"$set": {"record_status": "converted", "converted_order_id": order["provider_id"], "status_updated_at": now}},
    )
    return {phone for phone in affected if phone}


async def sync_shopify_org(org_id: str) -> None:
    token = secrets.token_urlsafe(16)
    lease_before = utcnow() - timedelta(minutes=CLAIM_LEASE_MINUTES)
    connection = await db.shopify_connections.find_one_and_update(
        {"org_id": org_id, "status": "connected", "sync_status": {"$in": ["pending", "failed", "syncing"]}, "next_sync_attempt_at": {"$not": {"$gt": utcnow()}}, "$or": [{"sync_claimed_at": {"$lt": lease_before}}, {"sync_claimed_at": None}, {"sync_status": {"$ne": "syncing"}}]},
        {"$set": {"sync_status": "syncing", "sync_claim_token": token, "sync_claimed_at": utcnow(), "sync_error": None}},
        return_document=ReturnDocument.AFTER,
    )
    if not connection:
        return
    full_sync = bool(connection.get("sync_full"))
    last_sync = _parse_datetime(connection.get("last_sync_at"))
    cutoff_time = _parse_datetime(connection.get("sync_cutoff_at")) or (utcnow() - timedelta(days=60) if full_sync or not last_sync else last_sync - timedelta(minutes=10))
    cursor_state: dict[str, Any] = dict(connection.get("sync_cursor") or {})
    completed = set(connection.get("sync_completed_resources") or [])
    counts = dict(connection.get("import_counts") or {"checkouts": 0, "orders": 0})
    activation = _parse_datetime(connection.get("automation_enabled_at") or connection.get("connected_at"))
    touched_phones: set[str] = set()
    await db.shopify_connections.update_one(
        {"org_id": org_id, "sync_claim_token": token},
        {"$set": {"sync_cutoff_at": cutoff_time}},
    )
    try:
        for kind, query, root in (("checkouts", CHECKOUTS_QUERY, "abandonedCheckouts"), ("orders", ORDERS_QUERY, "orders")):
            if kind in completed:
                continue
            cursor = cursor_state.get(kind)
            filter_field = "created_at" if kind == "checkouts" or full_sync or not last_sync else "updated_at"
            resource_cutoff = cutoff_time - timedelta(days=7) if kind == "checkouts" and not full_sync and last_sync else cutoff_time
            cutoff = resource_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
            sort_key = "CREATED_AT" if filter_field == "created_at" else "UPDATED_AT"
            while True:
                data = await _graphql(connection, query, {"first": 50, "after": cursor, "query": f"{filter_field}:>={cutoff}", "sortKey": sort_key}, timeout=30)
                page = data.get(root) or {}
                nodes = page.get("nodes") or []
                for node in nodes:
                    if kind == "checkouts":
                        updated = _parse_datetime(node.get("updatedAt")) or utcnow()
                        activity_time = _recovery_activity_time(updated, activation) if connection.get("automation_enabled") else None
                        historical = not activity_time
                        document = _checkout_from_graphql(org_id, connection["shop"], node, historical=historical, delay_minutes=int(connection.get("reminder_delay_minutes") or 30))
                        if activity_time and document.get("reminder_status") == "pending":
                            document["reminder_due_at"] = _reminder_due(activity_time, int(connection.get("reminder_delay_minutes") or 30))
                        if not any(document.get(field) for field in ("customer_id", "phone", "customer_name")):
                            continue
                        existing = await db.shopify_checkouts.find_one(
                            {"org_id": org_id, "provider_id": document["provider_id"]}, {"phone": 1},
                        )
                        if existing and existing.get("phone"):
                            touched_phones.add(existing["phone"])
                        if document.get("phone"):
                            touched_phones.add(document["phone"])
                        reminder_status = document.pop("reminder_status")
                        reminder_due_at = document.pop("reminder_due_at")
                        await _upsert_newer(
                            db.shopify_checkouts,
                            {"org_id": org_id, "provider_id": document["provider_id"]},
                            document,
                            on_insert={"reminder_status": reminder_status, "reminder_due_at": reminder_due_at, "imported_at": utcnow()},
                        )
                    else:
                        document = _order_from_graphql(org_id, connection["shop"], node)
                        await _upsert_newer(db.shopify_orders, {"org_id": org_id, "provider_id": document["provider_id"]}, document, on_insert={"imported_at": utcnow()})
                        touched_phones.update(await _mark_order_conversion(org_id, document))
                    counts[kind] = counts.get(kind, 0) + 1
                info = page.get("pageInfo") or {}
                cursor = info.get("endCursor")
                cursor_state[kind] = cursor
                await db.shopify_connections.update_one({"org_id": org_id, "sync_claim_token": token}, {"$set": {"sync_cursor": cursor_state, "import_counts": counts, "sync_claimed_at": utcnow()}})
                if not info.get("hasNextPage"):
                    completed.add(kind)
                    await db.shopify_connections.update_one(
                        {"org_id": org_id, "sync_claim_token": token},
                        {"$set": {"sync_completed_resources": sorted(completed), "sync_claimed_at": utcnow()}},
                    )
                    break
        if connection.get("whatsapp_id") and connection.get("template_name"):
            await list_compatible_templates(org_id, connection["whatsapp_id"], phone_id=connection.get("whatsapp_phone_id"))
            connection = await db.shopify_connections.find_one({"org_id": org_id, "sync_claim_token": token}) or connection
        await _repair_org_checkout_states(org_id, connection, None if full_sync else touched_phones)
        counts = {
            "checkouts": await db.shopify_checkouts.count_documents({"org_id": org_id}),
            "orders": await db.shopify_orders.count_documents({"org_id": org_id}),
        }
        await db.shopify_connections.update_one(
            {"org_id": org_id, "sync_claim_token": token},
            {"$set": {"sync_status": "complete", "last_sync_at": utcnow(), "sync_error": None, "sync_failures": 0, "last_error": None, "import_counts": counts, "updated_at": utcnow()}, "$unset": {"sync_claim_token": "", "sync_claimed_at": "", "sync_full": "", "sync_cursor": "", "sync_cutoff_at": "", "sync_filter_field": "", "sync_completed_resources": "", "next_sync_attempt_at": ""}},
        )
        await db.shopify_connections.update_one(
            {"org_id": org_id, "sync_status": "complete", "resync_requested": True},
            {"$set": {"sync_status": "pending", "sync_full": True, "sync_requested_at": utcnow(), "sync_cursor": {}, "sync_completed_resources": [], "import_counts": {"checkouts": 0, "orders": 0}}, "$unset": {"resync_requested": ""}},
        )
    except Exception as exc:
        logger.warning("Shopify sync failed for org suffix {}: {}", org_id[-6:], type(exc).__name__)
        failures = int(connection.get("sync_failures") or 0) + 1
        await db.shopify_connections.update_one(
            {"org_id": org_id, "sync_claim_token": token},
            {"$set": {"sync_status": "failed", "sync_error": "Shopify sync paused and will resume automatically", "sync_failures": failures, "next_sync_attempt_at": _sync_retry_at(failures), "updated_at": utcnow()}, "$unset": {"sync_claim_token": "", "sync_claimed_at": ""}},
        )
        await _admin_warning(org_id, "sync_failed")
    await _commerce_event(org_id, "sync", org_id)


async def process_shopify_syncs() -> None:
    await db.shopify_connections.update_many(
        {"status": "connected", "sync_status": "complete", "last_sync_at": {"$lte": utcnow() - timedelta(minutes=5)}},
        {"$set": {"sync_status": "pending", "sync_full": False, "sync_requested_at": utcnow(), "sync_cursor": {}, "sync_completed_resources": []}, "$unset": {"sync_cutoff_at": "", "sync_filter_field": "", "next_sync_attempt_at": ""}},
    )
    cursor = db.shopify_connections.find(
        {"status": "connected", "sync_status": {"$in": ["pending", "failed", "syncing"]}, "next_sync_attempt_at": {"$not": {"$gt": utcnow()}}},
        {"org_id": 1},
    ).sort([("next_sync_attempt_at", 1), ("sync_requested_at", 1)]).limit(5)
    async for item in cursor:
        await sync_shopify_org(item["org_id"])


def verify_webhook_hmac(raw_body: bytes, supplied: str) -> None:
    expected = base64.b64encode(hmac.new(SHOPIFY_CLIENT_SECRET.encode(), raw_body, hashlib.sha256).digest()).decode()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid Shopify webhook signature")


async def process_webhook(raw_body: bytes, headers: dict[str, str]) -> str:
    _require_configuration()
    verify_webhook_hmac(raw_body, headers.get("x-shopify-hmac-sha256", ""))
    shop = normalize_shop(headers.get("x-shopify-shop-domain", ""))
    webhook_id = (headers.get("x-shopify-webhook-id") or "").strip()
    topic = (headers.get("x-shopify-topic") or "").strip().lower()
    if not webhook_id or len(webhook_id) > 200:
        raise HTTPException(status_code=422, detail="Missing Shopify webhook ID")
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid Shopify webhook payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid Shopify webhook payload")
    claim_token = secrets.token_urlsafe(16)
    now = utcnow()
    try:
        await db.shopify_webhook_receipts.insert_one({
            "shop": shop, "webhook_id": webhook_id, "topic": topic, "status": "processing",
            "claim_token": claim_token, "claim_expires_at": now + timedelta(minutes=CLAIM_LEASE_MINUTES),
            "received_at": now,
        })
    except DuplicateKeyError:
        receipt = await db.shopify_webhook_receipts.find_one_and_update(
            {"shop": shop, "webhook_id": webhook_id, "status": "processing", "$or": [
                {"claim_expires_at": {"$lte": now}}, {"claim_expires_at": {"$exists": False}},
            ]},
            {"$set": {"claim_token": claim_token, "claim_expires_at": now + timedelta(minutes=CLAIM_LEASE_MINUTES)}},
            return_document=ReturnDocument.AFTER,
        )
        if not receipt:
            receipt = await db.shopify_webhook_receipts.find_one({"shop": shop, "webhook_id": webhook_id}, {"status": 1})
        if receipt and receipt.get("status") == "processed":
            return "duplicate"
        if not receipt or receipt.get("claim_token") != claim_token:
            raise HTTPException(status_code=503, detail="Shopify webhook is still processing")
    try:
        connection = await db.shopify_connections.find_one({"shop": shop, "status": "connected"})
        if not connection:
            await db.shopify_webhook_receipts.update_one(
                {"shop": shop, "webhook_id": webhook_id, "claim_token": claim_token},
                {"$set": {"status": "processed", "processed_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
            )
            return "ignored"
        event_time = _parse_datetime(headers.get("x-shopify-triggered-at")) or _parse_datetime(payload.get("updated_at")) or now
        await _apply_webhook(connection, topic, payload, event_time)
        completed = await db.shopify_webhook_receipts.update_one(
            {"shop": shop, "webhook_id": webhook_id, "claim_token": claim_token},
            {"$set": {"status": "processed", "processed_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
        )
        if not completed.matched_count:
            raise HTTPException(status_code=503, detail="Shopify webhook processing lease expired")
        return "processed"
    except Exception:
        await db.shopify_webhook_receipts.delete_one({"shop": shop, "webhook_id": webhook_id, "status": "processing", "claim_token": claim_token})
        raise


async def _apply_webhook(connection: dict, topic: str, payload: dict, event_time: datetime) -> None:
    org_id = connection["org_id"]
    if topic == "app/uninstalled":
        await disconnect_shopify(org_id, provider_uninstall=True)
        return
    if topic in {"customers/redact", "shop/redact"}:
        if topic == "shop/redact":
            await disconnect_shopify(org_id, provider_uninstall=True)
        else:
            customer_id = str(payload.get("customer", {}).get("id") or payload.get("customer_id") or "")
            await db.shopify_checkouts.delete_many({"org_id": org_id, "customer_id": {"$in": [customer_id, f"gid://shopify/Customer/{customer_id}"]}})
            await db.shopify_orders.delete_many({"org_id": org_id, "customer_id": {"$in": [customer_id, f"gid://shopify/Customer/{customer_id}"]}})
        return
    if topic in {"customers/data_request"}:
        return
    if topic.startswith("orders/"):
        if topic.endswith("delete"):
            provider_id = _provider_gid("Order", payload.get("admin_graphql_api_id") or payload.get("id"))
            await _upsert_newer(
                db.shopify_orders,
                {"org_id": org_id, "provider_id": provider_id},
                {"shop": connection["shop"], "record_status": "deleted", "provider_updated_at": event_time, "updated_at": event_time, "status_updated_at": utcnow()},
                on_insert={"created_at": event_time, "provider_created_at": event_time},
            )
            await _commerce_event(org_id, "order", provider_id)
            return
        order = _order_from_webhook(org_id, connection["shop"], payload)
        await _upsert_newer(db.shopify_orders, {"org_id": org_id, "provider_id": order["provider_id"]}, order)
        touched = await _mark_order_conversion(org_id, order)
        if touched:
            await _repair_org_checkout_states(org_id, connection, touched)
        await _commerce_event(org_id, "order", order["provider_id"])
        return
    if topic.startswith("checkouts/"):
        provider_id = _provider_gid("AbandonedCheckout", payload.get("admin_graphql_api_id") or payload.get("id") or payload.get("token"))
        checkout_token = str(payload.get("token") or "")
        if topic.endswith("delete"):
            identifiers = [{"provider_id": provider_id}]
            if checkout_token:
                identifiers.append({"checkout_token": checkout_token})
            deleted = await db.shopify_checkouts.find_one_and_update(
                {"org_id": org_id, "$or": identifiers, "provider_started_at": {"$exists": False}, "reminder_status": {"$nin": ["provider_accepted", "delivery_unknown", "sent"]}, "$and": [{"$or": [{"provider_updated_at": {"$lte": event_time}}, {"provider_updated_at": {"$exists": False}}]}]},
                {"$set": {"record_status": "deleted", "reminder_status": "cancelled", "failure_category": "checkout_deleted", "reminder_due_at": None, "updated_at": event_time, "provider_updated_at": event_time, "status_updated_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
                return_document=ReturnDocument.AFTER,
            )
            await db.shopify_checkouts.update_one(
                {"org_id": org_id, "$and": [{"$or": identifiers}, {"$or": [{"provider_started_at": {"$exists": True}}, {"reminder_status": {"$in": ["provider_accepted", "delivery_unknown", "sent"]}}]}, {"$or": [{"provider_updated_at": {"$lte": event_time}}, {"provider_updated_at": {"$exists": False}}]}]},
                {"$set": {"record_status": "deleted", "updated_at": event_time, "provider_updated_at": event_time, "status_updated_at": utcnow()}},
            )
            if deleted and deleted.get("phone"):
                await _reconcile_phone_checkouts(org_id, deleted["phone"], connection)
        else:
            checkout = _checkout_from_webhook(org_id, connection["shop"], payload, connection)
            if not any(checkout.get(field) for field in ("customer_id", "phone", "customer_name")):
                return
            if checkout.get("checkout_token"):
                imported = await db.shopify_checkouts.find_one(
                    {"org_id": org_id, "checkout_token": checkout["checkout_token"]}, {"provider_id": 1},
                )
                if imported:
                    checkout["provider_id"] = imported["provider_id"]
            key = {"org_id": org_id, "provider_id": checkout["provider_id"]}
            existing = await db.shopify_checkouts.find_one(key, {"phone": 1})
            existing_phone = (existing or {}).get("phone")
            provider_time = checkout["provider_updated_at"]
            update = await db.shopify_checkouts.update_one(
                {
                    **key,
                    "record_status": {"$ne": "deleted"},
                    "reminder_status": {"$nin": ["sent", "provider_accepted", "delivery_unknown", "converted"]},
                    "$nor": [{"reminder_status": "sending", "provider_started_at": {"$exists": True}}],
                    "$or": [{"provider_updated_at": {"$lte": provider_time}}, {"provider_updated_at": {"$exists": False}}],
                },
                {"$set": checkout, "$unset": {"claim_token": "", "claim_expires_at": "", "provider_started_at": ""}},
            )
            if not update.matched_count:
                existing = await db.shopify_checkouts.find_one(key, {"_id": 1})
                if existing:
                    provider_fields = {key: value for key, value in checkout.items() if key not in {"reminder_status", "reminder_due_at", "record_status", "completed_at"}}
                    if checkout.get("record_status") == "converted":
                        provider_fields.update({"record_status": "converted", "completed_at": checkout.get("completed_at")})
                    await _upsert_newer(db.shopify_checkouts, key, provider_fields)
                else:
                    try:
                        await db.shopify_checkouts.insert_one(checkout)
                    except DuplicateKeyError:
                        pass
            phones = {value for value in (existing_phone, checkout.get("phone")) if value}
            for phone in phones:
                await _reconcile_phone_checkouts(org_id, phone, connection)
        await _commerce_event(org_id, "checkout", provider_id)
        return
    if topic.startswith("customers/") or topic.startswith("customers_"):
        customer_id = _provider_gid("Customer", payload.get("admin_graphql_api_id") or payload.get("customer_id") or payload.get("id"))
        affected = set(await db.shopify_checkouts.distinct("phone", {"org_id": org_id, "customer_id": customer_id}))
        phone, consent, consent_at = _phone_and_consent(payload)
        consent_event = "whatsapp_marketing_consent" in topic or "whats_app_marketing_consent" in payload
        customer_updated = _parse_datetime(payload.get("updated_at")) or utcnow()
        update = {"customer_updated_at": customer_updated, "status_updated_at": utcnow()}
        if phone:
            update["phone"] = phone
        customer_name = _name(payload)
        if customer_name:
            update["customer_name"] = customer_name
        customer_filter: dict[str, Any] = {
            "org_id": org_id,
            "customer_id": customer_id,
            "$or": [{"customer_updated_at": {"$lte": customer_updated}}, {"customer_updated_at": {"$exists": False}}],
        }
        await db.shopify_checkouts.update_many(customer_filter, {"$set": update})
        await db.shopify_orders.update_many(customer_filter, {"$set": update})
        if consent_event:
            consent_time = consent_at or customer_updated
            consent_filter = {
                "org_id": org_id,
                "customer_id": customer_id,
                "$or": [{"consent_updated_at": {"$lte": consent_time}}, {"consent_updated_at": {"$exists": False}}],
            }
            await db.shopify_checkouts.update_many(consent_filter, {"$set": {"whatsapp_consent": consent, "consent_updated_at": consent_time, "status_updated_at": utcnow()}})
        if phone:
            affected.add(phone)
        await _repair_org_checkout_states(org_id, connection, {value for value in affected if value})
        await _commerce_event(org_id, "customer", customer_id)


def _provider_gid(kind: str, value: Any) -> str:
    text = str(value or "")
    return text if text.startswith("gid://") else f"gid://shopify/{kind}/{text}"


def _checkout_from_webhook(org_id: str, shop: str, payload: dict, connection: dict) -> dict:
    customer = payload.get("customer") or {}
    shipping_address = payload.get("shipping_address") or {}
    billing_address = payload.get("billing_address") or {}
    address = shipping_address or billing_address
    phone, consent, consent_at = _record_phone_and_consent(
        customer, shipping_address, billing_address, payload.get("phone"),
    )
    items = []
    for item in payload.get("line_items") or []:
        items.append({"id": str(item.get("admin_graphql_api_id") or item.get("id") or ""), "name": item.get("title") or item.get("name") or "Product", "quantity": int(item.get("quantity") or 0), "total": {"amount": float(item.get("line_price") or item.get("price") or 0), "currency": payload.get("currency") or ""}})
    updated = _parse_datetime(payload.get("updated_at")) or utcnow()
    customer_id = _provider_gid("Customer", customer.get("admin_graphql_api_id") or customer.get("id")) if customer.get("id") or customer.get("admin_graphql_api_id") else ""
    activation = _parse_datetime(connection.get("automation_enabled_at"))
    activity_time = _recovery_activity_time(updated, activation) if connection.get("automation_enabled") else None
    status = _eligibility_status(phone, items) if activity_time else ("cancelled" if not connection.get("automation_enabled") else "historical")
    if payload.get("completed_at"):
        status = "converted"
    return {
        "org_id": org_id, "shop": shop,
        "provider_id": _provider_gid("AbandonedCheckout", payload.get("admin_graphql_api_id") or payload.get("id") or payload.get("token")),
        "checkout_token": str(payload.get("token") or payload.get("id") or ""),
        "customer_id": customer_id,
        "customer_name": " ".join(part for part in (customer.get("first_name") or address.get("first_name"), customer.get("last_name") or address.get("last_name")) if part).strip(),
        "phone": phone, "whatsapp_consent": consent, "consent_updated_at": consent_at,
        "products": items, "total": {"amount": float(payload.get("total_price") or 0), "currency": payload.get("currency") or ""},
        "recovery_url": payload.get("abandoned_checkout_url") or payload.get("web_url") or "",
        "created_at": _parse_datetime(payload.get("created_at")) or updated, "provider_created_at": _parse_datetime(payload.get("created_at")) or updated,
        "updated_at": updated, "provider_updated_at": updated, "status_updated_at": utcnow(),
        "completed_at": _parse_datetime(payload.get("completed_at")), "record_status": "converted" if payload.get("completed_at") else "abandoned",
        "reminder_status": status, "reminder_due_at": None if status != "pending" else _reminder_due(activity_time, int(connection.get("reminder_delay_minutes") or 30)),
    }


def _order_from_webhook(org_id: str, shop: str, payload: dict) -> dict:
    customer = payload.get("customer") or {}
    shipping_address = payload.get("shipping_address") or {}
    billing_address = payload.get("billing_address") or {}
    address = shipping_address or billing_address
    phone, _, _ = _record_phone_and_consent(
        customer, shipping_address, billing_address, payload.get("phone"),
    )
    items = [{"id": str(item.get("admin_graphql_api_id") or item.get("id") or ""), "name": item.get("name") or item.get("title") or "Product", "quantity": int(item.get("quantity") or 0), "total": {"amount": float(item.get("price") or 0) * int(item.get("quantity") or 0), "currency": payload.get("currency") or ""}} for item in payload.get("line_items") or []]
    provider_id = _provider_gid("Order", payload.get("admin_graphql_api_id") or payload.get("id"))
    created = _parse_datetime(payload.get("created_at")) or utcnow()
    updated = _parse_datetime(payload.get("updated_at")) or created
    return {
        "org_id": org_id, "shop": shop, "provider_id": provider_id,
        "order_url": f"https://{shop}/admin/orders/{provider_id.rsplit('/', 1)[-1]}",
        "order_number": str(payload.get("name") or payload.get("order_number") or ""),
        "cart_token": str(payload.get("cart_token") or ""), "checkout_token": str(payload.get("checkout_token") or ""),
        "customer_id": _provider_gid("Customer", customer.get("admin_graphql_api_id") or customer.get("id")) if customer.get("id") or customer.get("admin_graphql_api_id") else "",
        "customer_name": " ".join(part for part in (customer.get("first_name") or address.get("first_name"), customer.get("last_name") or address.get("last_name")) if part).strip(),
        "phone": phone, "products": items, "total": {"amount": float(payload.get("total_price") or 0), "currency": payload.get("currency") or ""},
        "payment_status": str(payload.get("financial_status") or "").upper(), "fulfillment_status": str(payload.get("fulfillment_status") or "UNFULFILLED").upper(),
        "cancelled_at": _parse_datetime(payload.get("cancelled_at")), "record_status": "active",
        "created_at": created, "provider_created_at": created,
        "updated_at": updated, "provider_updated_at": updated, "status_updated_at": utcnow(),
    }


async def get_connection(org_id: str) -> dict:
    whatsapp_connections = await db.whatsapp_connections.find(
        {"org_id": org_id, "is_active": {"$ne": False}},
        {"_id": 0, "wa_id": 1, "phone_id": 1, "name": 1, "phone": 1, "automation_enabled": 1},
    ).to_list(length=20)
    connection = await db.shopify_connections.find_one({"org_id": org_id})
    if not connection:
        return {"connected": False, "configured": bool(SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET and SHOPIFY_PUBLIC_BASE_URL), "whatsapp_connections": whatsapp_connections}
    safe = {key: connection.get(key) for key in (
        "org_id", "shop", "status", "connected_at", "updated_at", "sync_status", "last_sync_at",
        "sync_error", "webhook_error", "reminder_error", "import_counts", "whatsapp_id", "whatsapp_phone_id", "template_name", "template_language",
        "template_status", "reminder_delay_minutes", "automation_enabled",
    ) if connection.get(key) is not None}
    for key, value in list(safe.items()):
        if isinstance(value, datetime):
            safe[key] = _iso_utc(value)
    safe["connected"] = connection.get("status") == "connected"
    safe["last_error"] = connection.get("reminder_error") or connection.get("webhook_error") or connection.get("sync_error") or connection.get("last_error")
    safe["configured"] = bool(SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET and SHOPIFY_PUBLIC_BASE_URL)
    safe["whatsapp_connections"] = whatsapp_connections
    safe["readiness"] = {
        "shopify": safe["connected"],
        "whatsapp": bool(await _selected_whatsapp_connection(connection, {"_id": 1})),
        "template": bool(connection.get("template_status") == "APPROVED"),
        "automation": bool(connection.get("automation_enabled")),
    }
    return safe


async def list_compatible_templates(org_id: str, wa_id: str | None = None, *, phone_id: str | None = None, raise_unavailable: bool = False) -> list[dict]:
    connection = await _whatsapp_connection_for_selector(
        org_id, phone_id=phone_id, wa_id=wa_id, projection={"wa_id": 1, "phone_id": 1, "access_token": 1},
    )
    if not connection:
        return []
    try:
        templates: list[dict] = []
        after = None
        async with httpx.AsyncClient(timeout=12) as client:
            for _ in range(10):
                params = {"fields": "name,language,status,category,components", "limit": 100}
                if after:
                    params["after"] = after
                response = await client.get(
                    f"https://graph.facebook.com/{WA_VERSION or 'v24.0'}/{connection['wa_id']}/message_templates",
                    headers={"Authorization": f"Bearer {connection['access_token']}"}, params=params,
                )
                response.raise_for_status()
                payload = response.json()
                templates.extend(payload.get("data") or [])
                after = ((payload.get("paging") or {}).get("cursors") or {}).get("after")
                if not after or not (payload.get("paging") or {}).get("next"):
                    break
    except (httpx.HTTPError, ValueError) as exc:
        if raise_unavailable:
            raise HTTPException(status_code=503, detail="Meta template status is temporarily unavailable") from exc
        return []
    selected_connection = await db.shopify_connections.find_one(
        {"org_id": org_id, "whatsapp_id": connection["wa_id"], "$or": [{"whatsapp_phone_id": connection["phone_id"]}, {"whatsapp_phone_id": {"$exists": False}}]}, {"template_name": 1, "template_language": 1},
    )
    if selected_connection and selected_connection.get("template_name"):
        current = next((item for item in templates if item.get("name") == selected_connection.get("template_name") and item.get("language") == selected_connection.get("template_language")), None)
        status = str((current or {}).get("status") or "UNAVAILABLE").upper()
        await db.shopify_connections.update_one(
            {"org_id": org_id, "whatsapp_id": connection["wa_id"], "$or": [{"whatsapp_phone_id": connection["phone_id"]}, {"whatsapp_phone_id": {"$exists": False}}], "template_name": selected_connection.get("template_name")},
            {"$set": {"template_status": status, "reminder_error": None if current and _template_compatible(current) else "The selected WhatsApp template is not approved and compatible", "updated_at": utcnow()}},
        )
    return [{"name": item.get("name"), "language": item.get("language"), "status": item.get("status"), "category": item.get("category")} for item in templates if _template_compatible(item)]


async def create_default_template(org_id: str, phone_id: str = "", language: str = "en", *, wa_id: str = "") -> dict:
    connection = await _whatsapp_connection_for_selector(
        org_id, phone_id=phone_id or None, wa_id=wa_id or None, projection={"wa_id": 1, "phone_id": 1, "access_token": 1},
    )
    if not connection:
        raise HTTPException(status_code=404, detail="Active WhatsApp connection not found")
    payload = {
        "name": "heidelai_abandoned_cart_v1",
        "language": language,
        "category": "MARKETING",
        "components": [{
            "type": "BODY",
            "text": "Hi {{1}}, you left {{2}} in your cart. Complete your order here: {{3}}",
            "example": {"body_text": [["Aarav", "Laptop stand, keyboard", "https://example.myshopify.com/checkouts/example"]]},
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://graph.facebook.com/{WA_VERSION or 'v24.0'}/{connection['wa_id']}/message_templates",
                headers={"Authorization": f"Bearer {connection['access_token']}", "Content-Type": "application/json"},
                json=payload,
            )
        if response.status_code not in {200, 201}:
            raise HTTPException(status_code=409, detail="Meta could not create the abandoned-cart template")
    except HTTPException:
        raise
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Meta is temporarily unavailable") from exc
    await db.shopify_connections.update_one(
        {"org_id": org_id, "status": "connected"},
        {"$set": {"whatsapp_id": connection["wa_id"], "whatsapp_phone_id": connection["phone_id"], "template_name": payload["name"], "template_language": language, "template_status": "PENDING", "updated_at": utcnow()}},
    )
    return {"name": payload["name"], "language": language, "status": "PENDING", "category": "MARKETING"}


def _template_compatible(template: dict) -> bool:
    if str(template.get("status") or "").upper() != "APPROVED" or str(template.get("category") or "").upper() != "MARKETING":
        return False
    components = template.get("components") or []
    bodies = [part for part in components if str(part.get("type") or "").upper() == "BODY"]
    if len(bodies) != 1 or any(re.search(r"{{\s*\d+\s*}}", json.dumps(part)) for part in components if part is not bodies[0]):
        return False
    placeholders = re.findall(r"{{\s*(\d+)\s*}}", str(bodies[0].get("text") or ""))
    return sorted(placeholders) == ["1", "2", "3"]


async def update_settings(org_id: str, settings: dict) -> dict:
    delay = int(settings.get("reminder_delay_minutes") or 30)
    if delay not in ALLOWED_DELAYS:
        raise HTTPException(status_code=422, detail="Unsupported reminder delay")
    requested_wa_id = str(settings.get("whatsapp_id") or "").strip()
    requested_phone_id = str(settings.get("whatsapp_phone_id") or "").strip()
    template_name = str(settings.get("template_name") or "").strip()
    template_language = str(settings.get("template_language") or "en").strip()
    enabled = bool(settings.get("automation_enabled"))
    whatsapp = await _whatsapp_connection_for_selector(
        org_id,
        phone_id=requested_phone_id or None,
        wa_id=requested_wa_id or None,
        projection={"wa_id": 1, "phone_id": 1, "automation_enabled": 1},
        require_automation=enabled,
    ) if requested_phone_id or requested_wa_id else None
    wa_id = str((whatsapp or {}).get("wa_id") or "")
    phone_id = str((whatsapp or {}).get("phone_id") or "")
    templates = await list_compatible_templates(org_id, wa_id or None, phone_id=phone_id or None, raise_unavailable=enabled) if template_name and whatsapp else []
    selected = next((item for item in templates if item["name"] == template_name and item["language"] == template_language), None)
    if enabled and (not phone_id or not selected):
        raise HTTPException(status_code=409, detail="Choose an active WhatsApp number and approved three-variable Marketing template")
    existing = await db.shopify_connections.find_one({"org_id": org_id, "status": "connected"})
    if not existing:
        raise HTTPException(status_code=404, detail="Connect Shopify before changing settings")
    settings_update = {
        "whatsapp_id": wa_id or None, "whatsapp_phone_id": phone_id or None, "template_name": template_name or None, "template_language": template_language,
        "template_status": "APPROVED" if selected else None, "reminder_delay_minutes": delay,
        "automation_enabled": enabled, "updated_at": utcnow(), "reminder_error": None, "last_error": None,
    }
    if enabled and (not existing.get("automation_enabled") or not existing.get("automation_enabled_at")):
        settings_update["automation_enabled_at"] = utcnow()
    result = await db.shopify_connections.update_one(
        {"org_id": org_id, "status": "connected"},
        {"$set": settings_update},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Connect Shopify before changing settings")
    connection = await db.shopify_connections.find_one({"org_id": org_id})
    if enabled:
        await _repair_org_checkout_states(org_id, connection)
    else:
        await db.shopify_checkouts.update_many(
            {"org_id": org_id, "$or": [{"reminder_status": {"$in": ["pending", "retryable"]}}, {"reminder_status": "sending", "provider_started_at": {"$exists": False}}]},
            {"$set": {"reminder_status": "cancelled", "failure_category": "automation_disabled", "reminder_due_at": None, "status_updated_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
        )
    await _commerce_event(org_id, "settings", org_id)
    return await get_connection(org_id)


async def disconnect_shopify(org_id: str, *, provider_uninstall: bool = False) -> None:
    now = utcnow()
    result = await db.shopify_connections.update_one(
        {"org_id": org_id, "status": {"$in": ["connected", "cleanup_pending"]}},
        {"$set": {"status": "cleanup_pending" if provider_uninstall else "connected", "cleanup_pending": provider_uninstall, "automation_enabled": False, "updated_at": now}},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Shopify is not connected")
    await db.shopify_checkouts.update_many(
        {"org_id": org_id, "$or": [
            {"reminder_status": {"$in": ["pending", "retryable"]}},
            {"reminder_status": "sending", "provider_started_at": {"$exists": False}},
        ]},
        {"$set": {"reminder_status": "cancelled", "failure_category": "shopify_disconnected", "reminder_due_at": None, "status_updated_at": now}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )
    accepted = db.shopify_checkouts.find({"org_id": org_id, "reminder_status": "provider_accepted"})
    async for checkout in accepted:
        await _reconcile_accepted(checkout)
    in_flight = await db.shopify_checkouts.find_one(
        {"org_id": org_id, "$or": [{"reminder_status": "sending", "provider_started_at": {"$exists": True}}, {"reminder_status": "provider_accepted"}]},
        {"_id": 1},
    )
    if in_flight:
        if provider_uninstall:
            return
        raise HTTPException(status_code=409, detail="A Shopify reminder is finishing. Retry disconnect shortly")
    await _finish_shopify_disconnect(org_id, provider_uninstall=provider_uninstall)


async def _finish_shopify_disconnect(org_id: str, *, provider_uninstall: bool = False) -> None:
    await db.shopify_connections.update_one(
        {"org_id": org_id, "status": {"$in": ["connected", "cleanup_pending"]}},
        {"$set": {"status": "disconnected", "updated_at": utcnow(), "last_error": "Shopify app was uninstalled" if provider_uninstall else None}, "$unset": {"shop": "", "access_token": "", "refresh_token": "", "token_expires_at": "", "sync_claim_token": "", "sync_claimed_at": "", "cleanup_pending": ""}},
    )
    await db.shopify_checkouts.delete_many({"org_id": org_id})
    await db.shopify_orders.delete_many({"org_id": org_id})
    await _commerce_event(org_id, "connection", org_id)


async def _finish_pending_disconnects() -> None:
    cursor = db.shopify_connections.find({"status": "cleanup_pending", "cleanup_pending": True}, {"org_id": 1}).limit(10)
    async for connection in cursor:
        if not await db.shopify_checkouts.find_one(
            {"org_id": connection["org_id"], "$or": [{"reminder_status": "sending", "provider_started_at": {"$exists": True}}, {"reminder_status": "provider_accepted"}]},
            {"_id": 1},
        ):
            await _finish_shopify_disconnect(connection["org_id"], provider_uninstall=True)


async def commerce_summary(org_id: str) -> dict:
    connection = await get_connection(org_id)
    if not connection.get("connected"):
        return {"connected": False, "counts": {"carts": 0, "orders": 0}, "connection": connection}
    carts, orders, pending, sent = await asyncio.gather(
        db.shopify_checkouts.count_documents({"org_id": org_id}), db.shopify_orders.count_documents({"org_id": org_id, "record_status": {"$ne": "deleted"}}),
        db.shopify_checkouts.count_documents({"org_id": org_id, "reminder_status": {"$in": ["pending", "retryable"]}}),
        db.shopify_checkouts.count_documents({"org_id": org_id, "delivery_status": {"$in": ["sent", "delivered", "read"]}}),
    )
    return {"connected": True, "counts": {"carts": carts, "orders": orders, "pending": pending, "sent": sent}, "connection": connection}


def _cursor_query(cursor: str | None, field: str = "provider_updated_at") -> dict:
    if not cursor:
        return {}
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        moment = _parse_datetime(decoded.get("time"))
        provider_id = str(decoded.get("id") or "")
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="Invalid pagination cursor")
    if not moment or not provider_id:
        raise HTTPException(status_code=422, detail="Invalid pagination cursor")
    return {"$or": [{field: {"$lt": moment}}, {field: moment, "provider_id": {"$lt": provider_id}}]}


async def list_records(org_id: str, record_type: str, *, status: str = "", search: str = "", start: datetime | None = None, end: datetime | None = None, cursor: str | None = None, limit: int = 30) -> dict:
    collection = db.shopify_checkouts if record_type == "checkout" else db.shopify_orders
    sort_field = "provider_updated_at"
    query: dict[str, Any] = {"org_id": org_id, **({"record_status": {"$ne": "deleted"}} if record_type == "order" else {}), **_cursor_query(cursor, sort_field)}
    if status:
        query["reminder_status" if record_type == "checkout" else "payment_status"] = status
    if start or end:
        query[sort_field] = {**({"$gte": start} if start else {}), **({"$lte": end} if end else {})}
    if search:
        value = re.escape(search.strip()[:80])
        query["$and"] = [{"$or": [{"customer_name": {"$regex": value, "$options": "i"}}, {"phone": {"$regex": value}}, {"products.name": {"$regex": value, "$options": "i"}}, {"order_number": {"$regex": value, "$options": "i"}}]}]
    items = await collection.find(query).sort([(sort_field, -1), ("provider_id", -1)]).limit(min(limit, 50) + 1).to_list(length=min(limit, 50) + 1)
    has_more = len(items) > min(limit, 50)
    items = items[: min(limit, 50)]
    next_cursor = None
    if has_more and items:
        value = json.dumps({"time": _provider_time(items[-1]).isoformat(), "id": items[-1]["provider_id"]}).encode()
        next_cursor = base64.urlsafe_b64encode(value).decode().rstrip("=")
    conversation_ids = {f"whatsapp_{_phone_key(item.get('phone'))}" for item in items if _phone_key(item.get("phone"))}
    existing_conversations = await db[f"conversations_{org_id}"].find(
        {"conversation_id": {"$in": list(conversation_ids)}}, {"conversation_id": 1},
    ).to_list(length=len(conversation_ids)) if conversation_ids else []
    existing_ids = {item["conversation_id"] for item in existing_conversations}
    safe_items = []
    for item in items:
        safe = _safe_document(item, include_url=True)
        conversation_id = f"whatsapp_{_phone_key(item.get('phone'))}" if _phone_key(item.get("phone")) else ""
        safe["conversation_id"] = conversation_id if conversation_id in existing_ids else None
        safe_items.append(safe)
    return {"items": safe_items, "next_cursor": next_cursor}


async def conversation_activity(org_id: str, conversation_id: str) -> dict:
    conversation = await db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id}, {"customer_id": 1, "platform": 1})
    if not conversation or conversation.get("platform") != "whatsapp":
        raise HTTPException(status_code=404, detail="WhatsApp conversation not found")
    phone = normalize_phone(conversation.get("customer_id"))
    if not phone:
        return {"checkout": None, "orders": []}
    checkout = await db.shopify_checkouts.find_one({"org_id": org_id, "phone": phone, "record_status": "abandoned"}, sort=[("provider_updated_at", -1), ("provider_id", -1)])
    orders = await db.shopify_orders.find({"org_id": org_id, "phone": phone, "record_status": {"$ne": "deleted"}}).sort("created_at", -1).limit(3).to_list(length=3)
    return {"checkout": _safe_document(checkout, include_url=True), "orders": [_safe_document(item) for item in orders]}


def _product_summary(products: list[dict]) -> str:
    names = [re.sub(r"\s+", " ", str(item.get("name") or "Product")).strip()[:80] for item in products if item.get("name")]
    summary = ", ".join(names[:3])
    if len(names) > 3:
        summary += f" +{len(names) - 3} more"
    return summary[:240]


async def _is_whatsapp_suppressed(org_id: str, phone: str) -> bool:
    phone_key = _phone_key(phone)
    if not phone_key:
        return True
    if await db.whatsapp_suppressions.find_one({"org_id": org_id, "phone": phone_key}, {"_id": 1}):
        return True
    message = await db[f"messages_{org_id}"].find_one(
        {"conversation_id": f"whatsapp_{phone_key}", "role": "customer", "mode": {"$ne": "private"}, "content": {"$regex": r"^\s*(?:stop(?:\s*all)?|unsubscribe|cancel|end|quit|start|subscribe)\s*[.!]?\s*$", "$options": "i"}},
        {"content": 1}, sort=[("timestamp", -1)],
    )
    if _message_preference((message or {}).get("content")) != "opted_out":
        return False
    now = utcnow()
    await db.whatsapp_suppressions.update_one(
        {"org_id": org_id, "phone": phone_key},
        {"$set": {"reason": "customer_opt_out", "source": "message_history", "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return True


async def process_shopify_reminders() -> None:
    now = utcnow()
    await db.shopify_checkouts.update_many(
        {"reminder_status": "sending", "claim_expires_at": {"$lte": now}, "provider_started_at": {"$exists": False}},
        {"$set": {"reminder_status": "retryable", "reminder_due_at": now, "status_updated_at": now}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )
    await db.shopify_checkouts.update_many(
        {"reminder_status": "sending", "claim_expires_at": {"$lte": now}, "provider_started_at": {"$exists": True}},
        {"$set": {"reminder_status": "delivery_unknown", "failure_category": "provider_outcome_unknown", "reminder_due_at": None, "status_updated_at": now}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )
    for _ in range(10):
        token = secrets.token_urlsafe(16)
        now = utcnow()
        checkout = await db.shopify_checkouts.find_one_and_update(
            {"reminder_status": {"$in": ["pending", "retryable"]}, "reminder_due_at": {"$lte": now}, "$or": [{"claim_expires_at": {"$lte": now}}, {"claim_expires_at": None}]},
            {"$set": {"reminder_status": "sending", "claim_token": token, "claim_expires_at": now + timedelta(minutes=CLAIM_LEASE_MINUTES), "claimed_at": now, "status_updated_at": now}},
            sort=[("reminder_due_at", 1)], return_document=ReturnDocument.AFTER,
        )
        if not checkout:
            break
        await _send_claimed_checkout(checkout, token)
    cursor = db.shopify_checkouts.find({"reminder_status": "provider_accepted"}).limit(10)
    async for checkout in cursor:
        try:
            await _reconcile_accepted(checkout)
        except Exception as exc:
            logger.warning("Shopify Inbox reconciliation failed for checkout suffix {}: {}", str(checkout.get("provider_id") or "")[-8:], type(exc).__name__)
    await _finish_pending_disconnects()


async def _fail_claim(checkout: dict, token: str, status: str, reason: str, *, retry: bool = False) -> None:
    update: dict[str, Any] = {"reminder_status": "retryable" if retry else status, "failure_category": reason, "status_updated_at": utcnow()}
    if retry:
        update["reminder_due_at"] = utcnow() + timedelta(minutes=5)
    else:
        update["reminder_due_at"] = None
    operation: dict[str, Any] = {"$set": update, "$unset": {"claim_token": "", "claim_expires_at": ""}}
    if retry:
        operation["$inc"] = {"pre_provider_attempts": 1}
    await db.shopify_checkouts.update_one({"_id": checkout["_id"], "claim_token": token}, operation)
    await _commerce_event(checkout["org_id"], "checkout", checkout["provider_id"])


async def _defer_claim(checkout: dict, token: str, due: datetime, reason: str, *, clear_provider_start: bool = False) -> None:
    unset = {"claim_token": "", "claim_expires_at": ""}
    if clear_provider_start:
        unset.update({"provider_started_at": "", "send_whatsapp_id": "", "send_phone_id": "", "send_template_name": "", "send_template_language": ""})
    await db.shopify_checkouts.update_one(
        {"_id": checkout["_id"], "claim_token": token, **({} if clear_provider_start else {"provider_started_at": {"$exists": False}})},
        {"$set": {"reminder_status": "pending", "failure_category": reason, "reminder_due_at": due, "status_updated_at": utcnow()}, "$unset": unset},
    )
    await _commerce_event(checkout["org_id"], "checkout", checkout["provider_id"])


async def _refresh_checkout_before_send(checkout: dict, token: str, connection: dict) -> dict | None:
    data = await _graphql(connection, CHECKOUT_NODE_QUERY, {"id": checkout["provider_id"]}, timeout=20)
    node = data.get("node")
    if not node:
        await _fail_claim(checkout, token, "cancelled", "checkout_unavailable")
        return None
    fresh = _checkout_from_graphql(
        checkout["org_id"], connection["shop"], node, historical=False,
        delay_minutes=int(connection.get("reminder_delay_minutes") or 30),
    )
    provider_fields = {key: value for key, value in fresh.items() if key not in {"reminder_status", "reminder_due_at", "status_updated_at"}}
    if fresh.get("record_status") == "converted":
        await db.shopify_checkouts.update_one({"_id": checkout["_id"], "claim_token": token}, {"$set": provider_fields})
        await _fail_claim(checkout, token, "converted", "order_created")
        return None
    if not fresh.get("products"):
        await db.shopify_checkouts.update_one({"_id": checkout["_id"], "claim_token": token}, {"$set": provider_fields})
        await _fail_claim(checkout, token, "cancelled", "checkout_empty")
        return None
    changed = any(fresh.get(key) != checkout.get(key) for key in ("provider_updated_at", "phone", "products", "recovery_url", "completed_at"))
    if changed:
        due = _reminder_due(fresh["provider_updated_at"], int(connection.get("reminder_delay_minutes") or 30))
        await db.shopify_checkouts.update_one(
            {"_id": checkout["_id"], "claim_token": token, "provider_started_at": {"$exists": False}},
            {"$set": {**provider_fields, "reminder_status": "pending", "reminder_due_at": due, "status_updated_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": "", "failure_category": ""}},
        )
        phones = {value for value in (checkout.get("phone"), fresh.get("phone")) if value}
        await _repair_org_checkout_states(checkout["org_id"], connection, phones)
        await _commerce_event(checkout["org_id"], "checkout", checkout["provider_id"])
        return None
    return {**checkout, **provider_fields}


async def _send_claimed_checkout(checkout: dict, token: str) -> None:
    org_id = checkout["org_id"]
    provider_started = False
    try:
        connection = await db.shopify_connections.find_one({"org_id": org_id, "status": "connected", "automation_enabled": True})
        if not connection:
            await _fail_claim(checkout, token, "cancelled", "automation_disabled")
            return
        latest = await db.shopify_checkouts.find_one(
            {"org_id": org_id, "phone": checkout.get("phone")},
            sort=[("provider_updated_at", -1), ("provider_created_at", -1), ("provider_id", -1)],
        ) if checkout.get("phone") else None
        if latest and latest["_id"] != checkout["_id"]:
            await _fail_claim(checkout, token, "superseded", "newer_checkout")
            return
        if latest and (latest.get("record_status") != "abandoned" or latest.get("completed_at")):
            await _fail_claim(checkout, token, "converted" if latest.get("record_status") == "converted" else "cancelled", "newer_checkout_unavailable")
            return
        order_matches = []
        if checkout.get("checkout_token"):
            order_matches.append({"cart_token": checkout["checkout_token"]})
            order_matches.append({"checkout_token": checkout["checkout_token"]})
        order_match = {"org_id": org_id, "$or": order_matches or [{"provider_id": "__none__"}]}
        if checkout.get("completed_at") or await db.shopify_orders.find_one(order_match, {"_id": 1}):
            await _fail_claim(checkout, token, "converted", "order_created")
            return
        refreshed = await _refresh_checkout_before_send(checkout, token, connection)
        if not refreshed:
            return
        checkout = refreshed
        phone = normalize_phone(checkout.get("phone"))
        if not phone:
            await _fail_claim(checkout, token, "phone_missing", "phone_missing")
            return
        if await _is_whatsapp_suppressed(org_id, phone):
            await _fail_claim(checkout, token, "opted_out", "customer_opt_out")
            return
        prior = await db.shopify_checkouts.find_one({"org_id": org_id, "phone": phone, "_id": {"$ne": checkout["_id"]}, "reminder_status": {"$in": ["sent", "delivery_unknown"]}, "provider_started_at": {"$gte": utcnow() - timedelta(hours=24)}}, {"provider_started_at": 1}, sort=[("provider_started_at", -1)])
        if prior:
            await _defer_claim(checkout, token, _parse_datetime(prior["provider_started_at"]) + timedelta(hours=24), "phone_frequency_cap")
            return
        wa = await _selected_whatsapp_connection(connection, {"wa_id": 1, "phone_id": 1, "access_token": 1})
        templates = await list_compatible_templates(org_id, connection.get("whatsapp_id"), phone_id=connection.get("whatsapp_phone_id"), raise_unavailable=True)
        template = next((item for item in templates if item["name"] == connection.get("template_name") and item["language"] == connection.get("template_language")), None)
        if not wa or not template:
            await _fail_claim(checkout, token, "configuration_failure", "whatsapp_or_template_unavailable")
            await _admin_warning(org_id, "reminder_configuration")
            return
        values = [str(checkout.get("customer_name") or "").split(" ", 1)[0][:80] or "there", _product_summary(checkout.get("products") or []), str(checkout.get("recovery_url") or "")[:2048]]
        if not values[1] or not values[2].startswith("https://"):
            await _fail_claim(checkout, token, "configuration_failure", "checkout_content_unavailable")
            return
        body = {
            "messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "template",
            "template": {"name": template["name"], "language": {"code": template["language"]}, "components": [{"type": "body", "parameters": [{"type": "text", "text": value} for value in values]}]},
        }
        # Final fence: checkout claim and connection settings must still be authoritative.
        current, current_connection, opted_out = await asyncio.gather(
            db.shopify_checkouts.find_one({"_id": checkout["_id"], "claim_token": token, "reminder_status": "sending", "record_status": "abandoned", "completed_at": None}),
            db.shopify_connections.find_one({"org_id": org_id, "status": "connected", "automation_enabled": True, "whatsapp_id": wa["wa_id"], "whatsapp_phone_id": wa["phone_id"], "template_name": template["name"], "template_language": template["language"], "template_status": "APPROVED"}, {"_id": 1}),
            _is_whatsapp_suppressed(org_id, phone),
        )
        if not current:
            return
        if not current_connection:
            await _fail_claim(checkout, token, "cancelled", "configuration_changed")
            return
        if opted_out:
            await _fail_claim(checkout, token, "opted_out", "customer_opt_out")
            return
        latest_current = await db.shopify_checkouts.find_one(
            {"org_id": org_id, "phone": current.get("phone")},
            sort=[("provider_updated_at", -1), ("provider_created_at", -1), ("provider_id", -1)],
        ) if current.get("phone") else None
        if latest_current and latest_current["_id"] != checkout["_id"]:
            await _fail_claim(checkout, token, "superseded", "newer_checkout")
            return
        if _checkout_changed_for_send(checkout, current):
            due = _reminder_due(current.get("provider_updated_at"), int(connection.get("reminder_delay_minutes") or 30))
            await _defer_claim(checkout, token, due, "checkout_changed")
            return
        frozen = {
            "send_whatsapp_id": wa["wa_id"], "send_phone_id": wa["phone_id"],
            "send_template_name": template["name"], "send_template_language": template["language"],
        }
        if not await db.shopify_connections.find_one({"org_id": org_id, "status": "connected", "automation_enabled": True, "whatsapp_id": wa["wa_id"], "whatsapp_phone_id": wa["phone_id"], "template_name": template["name"], "template_language": template["language"], "template_status": "APPROVED"}, {"_id": 1}):
            await _fail_claim(checkout, token, "cancelled", "configuration_changed")
            return
        started = await db.shopify_checkouts.find_one_and_update(
            {
                "_id": checkout["_id"], "claim_token": token, "reminder_status": "sending", "provider_started_at": {"$exists": False},
                "record_status": "abandoned", "completed_at": None, "provider_updated_at": checkout.get("provider_updated_at"),
                "phone": checkout.get("phone"), "recovery_url": checkout.get("recovery_url"),
            },
            {"$set": {"provider_started_at": utcnow(), **frozen}},
            return_document=ReturnDocument.AFTER,
        )
        if not started:
            return
        current_checkout, current_connection, current_wa, current_opted_out = await asyncio.gather(
            db.shopify_checkouts.find_one({"_id": checkout["_id"], "claim_token": token, "reminder_status": "sending"}),
            db.shopify_connections.find_one({"org_id": org_id, "status": "connected", "automation_enabled": True, "whatsapp_id": wa["wa_id"], "whatsapp_phone_id": wa["phone_id"], "template_name": template["name"], "template_language": template["language"], "template_status": "APPROVED"}, {"_id": 1}),
            db.whatsapp_connections.find_one({"org_id": org_id, "wa_id": wa["wa_id"], "phone_id": wa["phone_id"], "is_active": {"$ne": False}, "automation_enabled": {"$ne": False}}, {"_id": 1}),
            _is_whatsapp_suppressed(org_id, phone),
        )
        if not current_checkout:
            return
        latest_current = await db.shopify_checkouts.find_one(
            {"org_id": org_id, "phone": current_checkout.get("phone")},
            sort=[("provider_updated_at", -1), ("provider_created_at", -1), ("provider_id", -1)],
        ) if current_checkout.get("phone") else None
        is_latest = not latest_current or latest_current["_id"] == checkout["_id"]
        checkout_valid = is_latest and current_checkout.get("record_status") == "abandoned" and not current_checkout.get("completed_at") and not _checkout_changed_for_send(checkout, current_checkout)
        if not checkout_valid or not current_connection or not current_wa or current_opted_out:
            converted = current_checkout.get("record_status") == "converted" or bool(current_checkout.get("completed_at"))
            if not current_opted_out and not converted and is_latest and current_connection and current_wa:
                due = _reminder_due(current_checkout.get("provider_updated_at"), int(connection.get("reminder_delay_minutes") or 30))
                await _defer_claim(checkout, token, due, "checkout_changed", clear_provider_start=True)
                return
            reason = "customer_opt_out" if current_opted_out else ("order_created" if converted else ("newer_checkout" if not is_latest else "configuration_changed"))
            await db.shopify_checkouts.update_one(
                {"_id": checkout["_id"], "claim_token": token, "reminder_status": "sending", "record_status": current_checkout.get("record_status")},
                {"$set": {"reminder_status": "opted_out" if current_opted_out else ("converted" if converted else ("superseded" if not is_latest else "cancelled")), "failure_category": reason, "reminder_due_at": None, "status_updated_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": "", "provider_started_at": "", "send_whatsapp_id": "", "send_phone_id": "", "send_template_name": "", "send_template_language": ""}},
            )
            return
        provider_started = True
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(f"https://graph.facebook.com/{WA_VERSION or 'v24.0'}/{wa['phone_id']}/messages", headers={"Authorization": f"Bearer {wa['access_token']}", "Content-Type": "application/json"}, json=body)
        except httpx.RequestError:
            await _fail_claim(checkout, token, "delivery_unknown", "provider_outcome_unknown")
            return
        if response.status_code not in {200, 201}:
            logger.warning("Shopify reminder rejected with provider status {}", response.status_code)
            try:
                provider_error = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            except ValueError:
                provider_error = {}
            if _provider_reports_block(provider_error):
                await db.whatsapp_suppressions.update_one(
                    {"org_id": org_id, "phone": _phone_key(phone)},
                    {"$set": {"reason": "provider_block", "source": "send_response", "updated_at": utcnow()}, "$setOnInsert": {"created_at": utcnow()}},
                    upsert=True,
                )
                await _fail_claim(checkout, token, "opted_out", "provider_block")
            else:
                await _fail_claim(checkout, token, "failed", "provider_rejected")
            return
        try:
            message_id = response.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            await _fail_claim(checkout, token, "delivery_unknown", "provider_response_invalid")
            return
        accepted = None
        for attempt in range(3):
            try:
                accepted = await db.shopify_checkouts.find_one_and_update(
                    {"_id": checkout["_id"], "claim_token": token, "reminder_status": "sending"},
                    {"$set": {"reminder_status": "provider_accepted", "provider_message_id": message_id, "provider_accepted_at": utcnow(), "delivery_status": "accepted", "delivery_rank": 0, "status_updated_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
                    return_document=ReturnDocument.AFTER,
                )
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
        if accepted:
            await _reconcile_accepted(accepted)
    except HTTPException as exc:
        if provider_started:
            await _fail_claim(checkout, token, "delivery_unknown", "provider_outcome_unknown")
            return
        retry = exc.status_code in {502, 503, 504} and int(checkout.get("pre_provider_attempts") or 0) < 3
        await _fail_claim(checkout, token, "failed", "pre_provider_failure", retry=retry)
    except Exception as exc:
        logger.warning("Shopify reminder failed before completion: {}", type(exc).__name__)
        if provider_started:
            await _fail_claim(checkout, token, "delivery_unknown", "provider_outcome_unknown")
            return
        retry = int(checkout.get("pre_provider_attempts") or 0) < 3
        await _fail_claim(checkout, token, "failed", "pre_provider_failure", retry=retry)


async def _reconcile_accepted(checkout: dict) -> None:
    message_id = checkout.get("provider_message_id")
    if not message_id:
        return
    org_id = checkout["org_id"]
    phone = checkout.get("phone", "").lstrip("+")
    whatsapp_id = checkout.get("send_whatsapp_id")
    if not whatsapp_id:
        connection = await db.shopify_connections.find_one({"org_id": org_id}, {"whatsapp_id": 1})
        whatsapp_id = (connection or {}).get("whatsapp_id")
    if not whatsapp_id or not phone:
        return
    if not sync_db[f"messages_{org_id}"].find_one({"message_id": message_id}, {"_id": 1}):
        name = str(checkout.get("customer_name") or "there").split(" ", 1)[0]
        content = f"Hi {name}, you left {_product_summary(checkout.get('products') or [])} in your cart. Complete your order here: {checkout.get('recovery_url') or ''}"
        conversation_id = await create_or_update_whatsapp_conversation(
            org_id=org_id, recipient_id=whatsapp_id, customer_phone_no=phone,
            customer_name=checkout.get("customer_name") or phone, last_message=content,
            last_sender="agent", is_auto_followup=True, extra_updates={"last_message_source": "shopify_abandoned_checkout"},
            idempotency_key=message_id,
        )
        try:
            await store_whatsapp_message(
                org_id=org_id, content=content, conversation_id=conversation_id, customer_phone_no=phone,
                type="template", payload=None, recipient_id=whatsapp_id, role=MessageRole.AGENT,
                sender_name="Shopify recovery", message_id=message_id, status="sent",
                bot_context={"source": "shopify_abandoned_checkout", "checkout_id": checkout["provider_id"]},
            )
        except DuplicateKeyError:
            pass
    result = await db.shopify_checkouts.update_one(
        {"_id": checkout["_id"], "reminder_status": "provider_accepted", "provider_message_id": message_id},
        {"$set": {"reminder_status": "sent", "sent_at": checkout.get("provider_accepted_at") or utcnow(), "status_updated_at": utcnow()}},
    )
    if result.modified_count:
        await _commerce_event(org_id, "checkout", checkout["provider_id"])


async def update_shopify_reminder_delivery(org_id: str, message_id: str, status: str) -> None:
    delivery_status = str(status or "").lower()
    rank = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}.get(delivery_status)
    if rank is None:
        return
    now = utcnow()
    update: dict[str, Any] = {
        "delivery_status": delivery_status,
        "delivery_rank": rank,
        "status_updated_at": now,
    }
    if delivery_status == "failed":
        update.update({"reminder_status": "failed", "failure_category": "provider_delivery_failed", "failed_at": now})
    elif delivery_status in {"sent", "delivered", "read"}:
        update["reminder_status"] = "sent"
        update[f"{delivery_status}_at"] = now
    checkout = await db.shopify_checkouts.find_one_and_update(
        {
            "org_id": org_id,
            "provider_message_id": message_id,
            "$or": [{"delivery_rank": {"$lt": rank}}, {"delivery_rank": {"$exists": False}}],
        },
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if checkout:
        await _commerce_event(org_id, "checkout", checkout["provider_id"])


async def _commerce_event(org_id: str, record_type: str, record_id: str) -> None:
    await broadcast_org_ws(org_id, "shopify_commerce_updated", {"org_id": org_id, "record_type": record_type, "record_id": record_id})


async def _admin_warning(org_id: str, category: str) -> None:
    if category == "reminder_configuration":
        await db.shopify_connections.update_one(
            {"org_id": org_id, "status": "connected"},
            {"$set": {"reminder_error": "Shopify recovery needs an active WhatsApp number and approved template", "updated_at": utcnow()}},
        )
    await broadcast_org_admin_ws(org_id, "shopify_sync_warning", {"org_id": org_id, "record_type": "warning", "record_id": category})
