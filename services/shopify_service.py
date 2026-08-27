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


API_VERSION = "2026-07"
OAUTH_SCOPES = "read_orders,read_customers"
ALLOWED_DELAYS = {15, 30, 60, 120, 240, 1440}
SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
ACTIVE_REMINDERS = {"pending", "retryable", "sending", "provider_accepted"}
TERMINAL_REMINDERS = {
    "sent", "converted", "cancelled", "delivery_unknown", "permission_missing",
    "phone_missing", "configuration_failure", "failed", "historical", "superseded",
}
OAUTH_TTL_MINUTES = 10
WEBHOOK_TTL_DAYS = 30
CLAIM_LEASE_MINUTES = 5

db = get_async_mongo_db()
sync_db = get_mongo_db()


CHECKOUTS_QUERY = """
query ShopifyCheckouts($first: Int!, $after: String, $query: String!) {
  abandonedCheckouts(first: $first, after: $after, query: $query, sortKey: CREATED_AT) {
    nodes {
      id abandonedCheckoutUrl createdAt updatedAt completedAt
      customer {
        id firstName lastName
        defaultPhoneNumber {
          phoneNumber
          whatsAppMarketingConsent { state updatedAt }
        }
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

ORDERS_QUERY = """
query ShopifyOrders($first: Int!, $after: String, $query: String!) {
  orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT) {
    nodes {
      id name createdAt updatedAt cancelledAt cartToken checkoutToken
      displayFinancialStatus displayFulfillmentStatus
      customer {
        id firstName lastName
        defaultPhoneNumber {
          phoneNumber
          whatsAppMarketingConsent { state updatedAt }
        }
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

CUSTOMER_CONSENT_QUERY = """
query ShopifyCustomerConsent($id: ID!) {
  customer(id: $id) {
    id firstName lastName
    defaultPhoneNumber {
      phoneNumber
      whatsAppMarketingConsent { state updatedAt }
    }
  }
}
"""

WEBHOOK_CREATE_MUTATION = """
mutation ShopifyWebhook($topic: WebhookSubscriptionTopic!, $uri: URL!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: { uri: $uri, format: JSON }) {
    webhookSubscription { id topic }
    userErrors { field message }
  }
}
"""

WEBHOOK_TOPICS = (
    "CHECKOUTS_CREATE", "CHECKOUTS_UPDATE", "CHECKOUTS_DELETE",
    "ORDERS_CREATE", "ORDERS_UPDATED", "ORDERS_CANCELLED", "ORDERS_DELETE",
    "CUSTOMERS_UPDATE", "CUSTOMERS_WHATS_APP_MARKETING_CONSENT_UPDATE", "APP_UNINSTALLED",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def normalize_shop(value: str) -> str:
    shop = (value or "").strip().lower()
    shop = re.sub(r"^https?://", "", shop).split("/", 1)[0].rstrip(".")
    if not SHOP_RE.fullmatch(shop):
        raise HTTPException(status_code=422, detail="Enter a valid *.myshopify.com store domain")
    return shop


def normalize_phone(value: Any) -> str:
    candidate = re.sub(r"[^\d+]", "", str(value or "").strip())
    return candidate if PHONE_RE.fullmatch(candidate) else ""


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
        "provider_updated_at", "consent_updated_at", "attempts", "shop",
    }
    value = {key: item for key, item in document.items() if key not in internal}
    for key, item in list(value.items()):
        if isinstance(item, datetime):
            value[key] = item.isoformat()
    if not include_url:
        value.pop("recovery_url", None)
    return value


async def ensure_shopify_indexes() -> None:
    await db.shopify_oauth_states.create_index("expires_at", expireAfterSeconds=0)
    await db.shopify_connections.create_index("org_id", unique=True)
    await db.shopify_connections.create_index("shop", unique=True, sparse=True)
    await db.shopify_checkouts.create_index([("org_id", 1), ("provider_id", 1)], unique=True)
    await db.shopify_checkouts.create_index([("org_id", 1), ("phone", 1), ("updated_at", -1)])
    await db.shopify_checkouts.create_index([("org_id", 1), ("checkout_token", 1)])
    await db.shopify_checkouts.create_index([("org_id", 1), ("updated_at", -1), ("provider_id", -1)])
    await db.shopify_checkouts.create_index([("org_id", 1), ("reminder_status", 1), ("updated_at", -1)])
    await db.shopify_checkouts.create_index([("shop", 1), ("provider_id", 1)])
    await db.shopify_checkouts.create_index([("reminder_status", 1), ("reminder_due_at", 1)])
    await db.shopify_orders.create_index([("org_id", 1), ("provider_id", 1)], unique=True)
    await db.shopify_orders.create_index([("org_id", 1), ("phone", 1), ("created_at", -1)])
    await db.shopify_orders.create_index([("org_id", 1), ("updated_at", -1), ("provider_id", -1)])
    await db.shopify_orders.create_index([("org_id", 1), ("payment_status", 1), ("updated_at", -1)])
    await db.shopify_orders.create_index([("shop", 1), ("provider_id", 1)])
    await db.shopify_webhook_receipts.create_index("received_at", expireAfterSeconds=WEBHOOK_TTL_DAYS * 86400)
    await db.shopify_webhook_receipts.create_index([("shop", 1), ("webhook_id", 1)], unique=True)


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
        "sync_status": "pending", "sync_requested_at": now, "last_error": None,
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
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"https://{connection['shop']}/admin/api/{API_VERSION}/graphql.json",
                headers=headers, json={"query": query, "variables": variables},
            )
        if response.status_code == 429:
            retry = min(int(response.headers.get("Retry-After", "2") or 2), 30)
            await asyncio.sleep(max(retry, 1))
            raise HTTPException(status_code=503, detail="Shopify is busy; synchronization will resume")
        response.raise_for_status()
        payload = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Shopify GraphQL request failed: {}", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Shopify is temporarily unavailable") from exc
    if payload.get("errors"):
        logger.warning("Shopify GraphQL returned {} error(s)", len(payload["errors"]))
        raise HTTPException(status_code=502, detail="Shopify could not complete the request")
    return payload.get("data") or {}


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
        await db.shopify_connections.update_one({"org_id": connection["org_id"]}, {"$set": {"last_error": "Some Shopify webhooks require attention", "updated_at": utcnow()}})
        await _admin_warning(connection["org_id"], "webhook_configuration")


def _checkout_from_graphql(org_id: str, shop: str, node: dict, historical: bool) -> dict:
    customer = node.get("customer") or {}
    phone, consent, consent_at = _record_phone_and_consent(
        customer, node.get("shippingAddress"), node.get("billingAddress"),
    )
    items = []
    for item in ((node.get("lineItems") or {}).get("nodes") or []):
        items.append({"id": item.get("id"), "name": item.get("title") or "Product", "quantity": int(item.get("quantity") or 0), "total": _money(item.get("originalTotalPriceSet"))})
    updated = _parse_datetime(node.get("updatedAt")) or utcnow()
    recovery_url = str(node.get("abandonedCheckoutUrl") or "")
    token_match = re.search(r"/([^/?]+)/recover(?:[/?]|$)", recovery_url)
    return {
        "org_id": org_id, "shop": shop, "provider_id": str(node.get("id")),
        "checkout_token": token_match.group(1) if token_match else "",
        "customer_id": customer.get("id"), "customer_name": _name(customer), "phone": phone,
        "whatsapp_consent": consent, "consent_updated_at": consent_at,
        "products": items, "total": _money(node.get("totalPriceSet")),
        "recovery_url": recovery_url,
        "created_at": _parse_datetime(node.get("createdAt")) or updated,
        "updated_at": updated, "provider_updated_at": updated, "completed_at": _parse_datetime(node.get("completedAt")),
        "record_status": "converted" if node.get("completedAt") else "abandoned",
        "reminder_status": "historical" if historical else _eligibility_status(phone, consent, items),
        "reminder_due_at": None if historical else updated + timedelta(minutes=30),
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
    return {
        "org_id": org_id, "shop": shop, "provider_id": provider_id, "order_number": node.get("name") or "",
        "order_url": f"https://{shop}/admin/orders/{provider_id.rsplit('/', 1)[-1]}",
        "cart_token": node.get("cartToken") or "", "checkout_token": node.get("checkoutToken") or "",
        "customer_id": customer.get("id"), "customer_name": _name(customer), "phone": phone,
        "products": items, "total": _money(node.get("totalPriceSet")),
        "payment_status": node.get("displayFinancialStatus") or "", "fulfillment_status": node.get("displayFulfillmentStatus") or "",
        "cancelled_at": _parse_datetime(node.get("cancelledAt")), "created_at": _parse_datetime(node.get("createdAt")) or utcnow(),
        "updated_at": _parse_datetime(node.get("updatedAt")) or utcnow(),
        "provider_updated_at": _parse_datetime(node.get("updatedAt")) or utcnow(),
    }


def _eligibility_status(phone: str, consent: str, items: list) -> str:
    if not items:
        return "cancelled"
    if not phone:
        return "phone_missing"
    if consent != "SUBSCRIBED":
        return "permission_missing"
    return "pending"


async def request_sync(org_id: str) -> None:
    result = await db.shopify_connections.update_one(
        {"org_id": org_id, "status": "connected"},
        {"$set": {"sync_status": "pending", "sync_requested_at": utcnow(), "sync_cursor": {}, "import_counts": {"checkouts": 0, "orders": 0}, "last_error": None, "updated_at": utcnow()}},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Connect Shopify before synchronizing")
    await _commerce_event(org_id, "sync", org_id)


async def _upsert_newer(collection, key: dict, fields: dict, *, on_insert: dict | None = None) -> bool:
    provider_time = fields.get("provider_updated_at") or utcnow()
    result = await collection.update_one(
        {**key, "$or": [{"provider_updated_at": {"$lte": provider_time}}, {"provider_updated_at": {"$exists": False}}]},
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


async def sync_shopify_org(org_id: str) -> None:
    token = secrets.token_urlsafe(16)
    lease_before = utcnow() - timedelta(minutes=CLAIM_LEASE_MINUTES)
    connection = await db.shopify_connections.find_one_and_update(
        {"org_id": org_id, "status": "connected", "sync_status": {"$in": ["pending", "failed", "syncing"]}, "$or": [{"sync_claimed_at": {"$lt": lease_before}}, {"sync_claimed_at": None}, {"sync_status": {"$ne": "syncing"}}]},
        {"$set": {"sync_status": "syncing", "sync_claim_token": token, "sync_claimed_at": utcnow(), "last_error": None}},
        return_document=ReturnDocument.AFTER,
    )
    if not connection:
        return
    cutoff = (utcnow() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor_state = connection.get("sync_cursor") or {}
    counts = connection.get("import_counts") or {"checkouts": 0, "orders": 0}
    try:
        for kind, query, root in (("checkouts", CHECKOUTS_QUERY, "abandonedCheckouts"), ("orders", ORDERS_QUERY, "orders")):
            cursor = cursor_state.get(kind)
            while True:
                data = await _graphql(connection, query, {"first": 50, "after": cursor, "query": f"created_at:>={cutoff}"}, timeout=30)
                page = data.get(root) or {}
                nodes = page.get("nodes") or []
                for node in nodes:
                    if kind == "checkouts":
                        document = _checkout_from_graphql(org_id, connection["shop"], node, historical=True)
                        if not any(document.get(field) for field in ("customer_id", "phone", "customer_name")):
                            continue
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
                    counts[kind] = counts.get(kind, 0) + 1
                info = page.get("pageInfo") or {}
                cursor = info.get("endCursor")
                cursor_state[kind] = cursor
                await db.shopify_connections.update_one({"org_id": org_id, "sync_claim_token": token}, {"$set": {"sync_cursor": cursor_state, "import_counts": counts, "sync_claimed_at": utcnow()}})
                if not info.get("hasNextPage"):
                    break
        await db.shopify_connections.update_one(
            {"org_id": org_id, "sync_claim_token": token},
            {"$set": {"sync_status": "complete", "last_sync_at": utcnow(), "last_error": None, "updated_at": utcnow()}, "$unset": {"sync_claim_token": "", "sync_claimed_at": ""}},
        )
    except Exception as exc:
        logger.warning("Shopify sync failed for org suffix {}: {}", org_id[-6:], type(exc).__name__)
        await db.shopify_connections.update_one(
            {"org_id": org_id, "sync_claim_token": token},
            {"$set": {"sync_status": "failed", "last_error": "Shopify sync paused and will resume automatically", "updated_at": utcnow()}, "$unset": {"sync_claim_token": "", "sync_claimed_at": ""}},
        )
        await _admin_warning(org_id, "sync_failed")
    await _commerce_event(org_id, "sync", org_id)


async def process_shopify_syncs() -> None:
    cursor = db.shopify_connections.find({"status": "connected", "sync_status": {"$in": ["pending", "failed", "syncing"]}}, {"org_id": 1}).limit(5)
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
        await _apply_webhook(connection, topic, payload)
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


async def _apply_webhook(connection: dict, topic: str, payload: dict) -> None:
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
            await db.shopify_orders.delete_one({"org_id": org_id, "provider_id": provider_id})
            await _commerce_event(org_id, "order", provider_id)
            return
        order = _order_from_webhook(org_id, connection["shop"], payload)
        await _upsert_newer(db.shopify_orders, {"org_id": org_id, "provider_id": order["provider_id"]}, order)
        match = {"org_id": org_id, "reminder_status": {"$in": list(ACTIVE_REMINDERS)}}
        tokens = [value for value in {order.get("cart_token"), order.get("checkout_token"), payload.get("checkout_token")} if value]
        if tokens:
            match["checkout_token"] = {"$in": tokens}
        else:
            match["provider_id"] = "__none__"
        await db.shopify_checkouts.update_many(
            {**match, "$or": [
                {"reminder_status": {"$in": ["pending", "retryable"]}},
                {"reminder_status": "sending", "provider_started_at": {"$exists": False}},
            ]},
            {"$set": {"record_status": "converted", "reminder_status": "converted", "converted_order_id": order["provider_id"], "updated_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
        )
        await db.shopify_checkouts.update_many(match, {"$set": {"record_status": "converted", "converted_order_id": order["provider_id"], "updated_at": utcnow()}})
        await _commerce_event(org_id, "order", order["provider_id"])
        return
    if topic.startswith("checkouts/"):
        provider_id = _provider_gid("AbandonedCheckout", payload.get("admin_graphql_api_id") or payload.get("id") or payload.get("token"))
        checkout_token = str(payload.get("token") or "")
        if topic.endswith("delete"):
            identifiers = [{"provider_id": provider_id}]
            if checkout_token:
                identifiers.append({"checkout_token": checkout_token})
            await db.shopify_checkouts.update_one(
                {"org_id": org_id, "$or": identifiers},
                {"$set": {"record_status": "deleted", "reminder_status": "cancelled", "updated_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
            )
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
            provider_time = checkout["provider_updated_at"]
            update = await db.shopify_checkouts.update_one(
                {
                    **key,
                    "reminder_status": {"$nin": ["sent", "provider_accepted", "delivery_unknown", "converted"]},
                    "$nor": [{"reminder_status": "sending", "provider_started_at": {"$exists": True}}],
                    "$or": [{"provider_updated_at": {"$lte": provider_time}}, {"provider_updated_at": {"$exists": False}}],
                },
                {"$set": checkout, "$unset": {"claim_token": "", "claim_expires_at": "", "provider_started_at": ""}},
            )
            if not update.matched_count:
                existing = await db.shopify_checkouts.find_one(key, {"_id": 1})
                if existing:
                    provider_fields = {key: value for key, value in checkout.items() if key not in {"reminder_status", "reminder_due_at"}}
                    await _upsert_newer(db.shopify_checkouts, key, provider_fields)
                else:
                    try:
                        await db.shopify_checkouts.insert_one(checkout)
                    except DuplicateKeyError:
                        pass
        await _commerce_event(org_id, "checkout", provider_id)
        return
    if topic.startswith("customers/") or topic.startswith("customers_"):
        customer_id = _provider_gid("Customer", payload.get("admin_graphql_api_id") or payload.get("customer_id") or payload.get("id"))
        phone, consent, consent_at = _phone_and_consent(payload)
        consent_event = "whatsapp_marketing_consent" in topic or "whats_app_marketing_consent" in payload
        update = {"updated_at": utcnow()}
        if phone:
            update["phone"] = phone
        customer_name = _name(payload)
        if customer_name:
            update["customer_name"] = customer_name
        if consent_event:
            update.update({"whatsapp_consent": consent, "consent_updated_at": consent_at})
        consent_filter: dict[str, Any] = {"org_id": org_id, "customer_id": customer_id}
        if consent_event and consent_at:
            consent_filter["$or"] = [{"consent_updated_at": {"$lte": consent_at}}, {"consent_updated_at": {"$exists": False}}]
        await db.shopify_checkouts.update_many(consent_filter, {"$set": update})
        await db.shopify_orders.update_many({"org_id": org_id, "customer_id": customer_id}, {"$set": {key: value for key, value in update.items() if key in {"phone", "customer_name", "updated_at"}}})
        if consent_event and consent != "SUBSCRIBED":
            await db.shopify_checkouts.update_many(
                {"org_id": org_id, "customer_id": customer_id, "$or": [
                    {"reminder_status": {"$in": ["pending", "retryable"]}},
                    {"reminder_status": "sending", "provider_started_at": {"$exists": False}},
                ]},
                {"$set": {"reminder_status": "permission_missing", "reminder_due_at": None}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
            )
        elif consent_event and connection.get("automation_enabled"):
            await db.shopify_checkouts.update_many(
                {"org_id": org_id, "customer_id": customer_id, "record_status": "abandoned", "reminder_status": "permission_missing", "phone": {"$ne": ""}, "products.0": {"$exists": True}},
                {"$set": {"reminder_status": "pending", "reminder_due_at": utcnow() + timedelta(minutes=int(connection.get("reminder_delay_minutes") or 30))}},
            )
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
    status = _eligibility_status(phone, consent, items) if connection.get("automation_enabled") else "cancelled"
    # Checkout webhooks can omit channel consent. Queue the check, but the final send fence still requires live SUBSCRIBED consent.
    if status == "permission_missing" and consent == "UNKNOWN" and customer_id and phone and items:
        status = "pending"
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
        "created_at": _parse_datetime(payload.get("created_at")) or updated, "updated_at": updated,
        "provider_updated_at": updated,
        "completed_at": _parse_datetime(payload.get("completed_at")), "record_status": "converted" if payload.get("completed_at") else "abandoned",
        "reminder_status": status, "reminder_due_at": None if status != "pending" else updated + timedelta(minutes=int(connection.get("reminder_delay_minutes") or 30)),
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
    return {
        "org_id": org_id, "shop": shop, "provider_id": provider_id,
        "order_url": f"https://{shop}/admin/orders/{provider_id.rsplit('/', 1)[-1]}",
        "order_number": str(payload.get("name") or payload.get("order_number") or ""),
        "cart_token": str(payload.get("cart_token") or ""), "checkout_token": str(payload.get("checkout_token") or ""),
        "customer_id": _provider_gid("Customer", customer.get("admin_graphql_api_id") or customer.get("id")) if customer.get("id") or customer.get("admin_graphql_api_id") else "",
        "customer_name": " ".join(part for part in (customer.get("first_name") or address.get("first_name"), customer.get("last_name") or address.get("last_name")) if part).strip(),
        "phone": phone, "products": items, "total": {"amount": float(payload.get("total_price") or 0), "currency": payload.get("currency") or ""},
        "payment_status": str(payload.get("financial_status") or "").upper(), "fulfillment_status": str(payload.get("fulfillment_status") or "UNFULFILLED").upper(),
        "cancelled_at": _parse_datetime(payload.get("cancelled_at")), "created_at": _parse_datetime(payload.get("created_at")) or utcnow(), "updated_at": _parse_datetime(payload.get("updated_at")) or utcnow(),
        "provider_updated_at": _parse_datetime(payload.get("updated_at")) or utcnow(),
    }


async def get_connection(org_id: str) -> dict:
    whatsapp_connections = await db.whatsapp_connections.find(
        {"org_id": org_id, "is_active": {"$ne": False}},
        {"_id": 0, "wa_id": 1, "name": 1, "phone": 1},
    ).to_list(length=20)
    connection = await db.shopify_connections.find_one({"org_id": org_id})
    if not connection:
        return {"connected": False, "configured": bool(SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET and SHOPIFY_PUBLIC_BASE_URL), "whatsapp_connections": whatsapp_connections}
    safe = {key: connection.get(key) for key in (
        "org_id", "shop", "status", "connected_at", "updated_at", "sync_status", "last_sync_at",
        "last_error", "import_counts", "whatsapp_id", "template_name", "template_language",
        "template_status", "reminder_delay_minutes", "automation_enabled",
    ) if connection.get(key) is not None}
    for key, value in list(safe.items()):
        if isinstance(value, datetime):
            safe[key] = value.isoformat()
    safe["connected"] = connection.get("status") == "connected"
    safe["configured"] = bool(SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET and SHOPIFY_PUBLIC_BASE_URL)
    safe["whatsapp_connections"] = whatsapp_connections
    safe["readiness"] = {
        "shopify": safe["connected"],
        "whatsapp": bool(connection.get("whatsapp_id") and await db.whatsapp_connections.find_one({"org_id": org_id, "wa_id": connection.get("whatsapp_id"), "is_active": {"$ne": False}}, {"_id": 1})),
        "template": bool(connection.get("template_status") == "APPROVED"),
        "automation": bool(connection.get("automation_enabled")),
    }
    return safe


async def list_compatible_templates(org_id: str, wa_id: str | None = None, *, raise_unavailable: bool = False) -> list[dict]:
    query = {"org_id": org_id, "is_active": {"$ne": False}}
    if wa_id:
        query["wa_id"] = wa_id
    connection = await db.whatsapp_connections.find_one(query, {"wa_id": 1, "access_token": 1})
    if not connection:
        return []
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(
                f"https://graph.facebook.com/{WA_VERSION or 'v24.0'}/{connection['wa_id']}/message_templates",
                headers={"Authorization": f"Bearer {connection['access_token']}"},
                params={"fields": "name,language,status,category,components", "limit": 100},
            )
        response.raise_for_status()
        templates = response.json().get("data") or []
    except (httpx.HTTPError, ValueError) as exc:
        if raise_unavailable:
            raise HTTPException(status_code=503, detail="Meta template status is temporarily unavailable") from exc
        return []
    return [{"name": item.get("name"), "language": item.get("language"), "status": item.get("status"), "category": item.get("category")} for item in templates if _template_compatible(item)]


async def create_default_template(org_id: str, wa_id: str, language: str = "en") -> dict:
    connection = await db.whatsapp_connections.find_one(
        {"org_id": org_id, "wa_id": wa_id, "is_active": {"$ne": False}},
        {"wa_id": 1, "access_token": 1},
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
                f"https://graph.facebook.com/{WA_VERSION or 'v24.0'}/{wa_id}/message_templates",
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
        {"$set": {"whatsapp_id": wa_id, "template_name": payload["name"], "template_language": language, "template_status": "PENDING", "updated_at": utcnow()}},
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
    wa_id = str(settings.get("whatsapp_id") or "").strip()
    template_name = str(settings.get("template_name") or "").strip()
    template_language = str(settings.get("template_language") or "en").strip()
    enabled = bool(settings.get("automation_enabled"))
    templates = await list_compatible_templates(org_id, wa_id or None) if template_name else []
    selected = next((item for item in templates if item["name"] == template_name and item["language"] == template_language), None)
    if enabled and (not wa_id or not selected):
        raise HTTPException(status_code=409, detail="Choose an active WhatsApp number and approved three-variable Marketing template")
    result = await db.shopify_connections.update_one(
        {"org_id": org_id, "status": "connected"},
        {"$set": {
            "whatsapp_id": wa_id or None, "template_name": template_name or None, "template_language": template_language,
            "template_status": "APPROVED" if selected else None, "reminder_delay_minutes": delay,
            "automation_enabled": enabled, "updated_at": utcnow(), "last_error": None,
        }},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Connect Shopify before changing settings")
    if enabled:
        next_due = utcnow() + timedelta(minutes=delay)
        await db.shopify_checkouts.update_many(
            {"org_id": org_id, "reminder_status": {"$in": ["pending", "retryable"]}},
            {"$set": {"reminder_due_at": next_due}},
        )
        await db.shopify_checkouts.update_many(
            {"org_id": org_id, "reminder_status": "sending", "provider_started_at": {"$exists": False}},
            {"$set": {"reminder_status": "pending", "reminder_due_at": next_due}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
        )
    else:
        await db.shopify_checkouts.update_many(
            {"org_id": org_id, "$or": [{"reminder_status": {"$in": ["pending", "retryable"]}}, {"reminder_status": "sending", "provider_started_at": {"$exists": False}}]},
            {"$set": {"reminder_status": "cancelled", "reminder_due_at": None}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
        )
    await _commerce_event(org_id, "settings", org_id)
    return await get_connection(org_id)


async def disconnect_shopify(org_id: str, *, provider_uninstall: bool = False) -> None:
    now = utcnow()
    result = await db.shopify_connections.update_one(
        {"org_id": org_id, "status": {"$in": ["connected", "disconnecting"]}},
        {"$set": {"status": "disconnecting", "automation_enabled": False, "updated_at": now}},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Shopify is not connected")
    await db.shopify_checkouts.update_many(
        {"org_id": org_id, "$or": [
            {"reminder_status": {"$in": ["pending", "retryable"]}},
            {"reminder_status": "sending", "provider_started_at": {"$exists": False}},
        ]},
        {"$set": {"reminder_status": "cancelled", "reminder_due_at": None, "updated_at": now}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )
    accepted = db.shopify_checkouts.find({"org_id": org_id, "reminder_status": "provider_accepted"})
    async for checkout in accepted:
        await _reconcile_accepted(checkout)
    if await db.shopify_checkouts.find_one({"org_id": org_id, "reminder_status": "sending", "provider_started_at": {"$exists": True}}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="A Shopify reminder is finishing. Retry disconnect shortly")
    await db.shopify_connections.update_one({"org_id": org_id, "status": "disconnecting"}, {"$set": {"status": "disconnected", "updated_at": now, "last_error": "Shopify app was uninstalled" if provider_uninstall else None}, "$unset": {"shop": "", "access_token": "", "refresh_token": "", "token_expires_at": "", "sync_claim_token": "", "sync_claimed_at": ""}})
    await db.shopify_checkouts.delete_many({"org_id": org_id})
    await db.shopify_orders.delete_many({"org_id": org_id})
    await _commerce_event(org_id, "connection", org_id)


async def commerce_summary(org_id: str) -> dict:
    connection = await get_connection(org_id)
    if not connection.get("connected"):
        return {"connected": False, "counts": {"carts": 0, "orders": 0}, "connection": connection}
    carts, orders, pending, sent = await asyncio.gather(
        db.shopify_checkouts.count_documents({"org_id": org_id}), db.shopify_orders.count_documents({"org_id": org_id}),
        db.shopify_checkouts.count_documents({"org_id": org_id, "reminder_status": {"$in": ["pending", "retryable"]}}),
        db.shopify_checkouts.count_documents({"org_id": org_id, "reminder_status": "sent"}),
    )
    return {"connected": True, "counts": {"carts": carts, "orders": orders, "pending": pending, "sent": sent}, "connection": connection}


def _cursor_query(cursor: str | None) -> dict:
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
    return {"$or": [{"updated_at": {"$lt": moment}}, {"updated_at": moment, "provider_id": {"$lt": provider_id}}]}


async def list_records(org_id: str, record_type: str, *, status: str = "", search: str = "", start: datetime | None = None, end: datetime | None = None, cursor: str | None = None, limit: int = 30) -> dict:
    collection = db.shopify_checkouts if record_type == "checkout" else db.shopify_orders
    query: dict[str, Any] = {"org_id": org_id, **_cursor_query(cursor)}
    if status:
        query["reminder_status" if record_type == "checkout" else "payment_status"] = status
    if start or end:
        query["updated_at"] = {**({"$gte": start} if start else {}), **({"$lte": end} if end else {})}
    if search:
        value = re.escape(search.strip()[:80])
        query["$and"] = [{"$or": [{"customer_name": {"$regex": value, "$options": "i"}}, {"phone": {"$regex": value}}, {"products.name": {"$regex": value, "$options": "i"}}, {"order_number": {"$regex": value, "$options": "i"}}]}]
    items = await collection.find(query).sort([("updated_at", -1), ("provider_id", -1)]).limit(min(limit, 50) + 1).to_list(length=min(limit, 50) + 1)
    has_more = len(items) > min(limit, 50)
    items = items[: min(limit, 50)]
    next_cursor = None
    if has_more and items:
        value = json.dumps({"time": items[-1]["updated_at"].isoformat(), "id": items[-1]["provider_id"]}).encode()
        next_cursor = base64.urlsafe_b64encode(value).decode().rstrip("=")
    return {"items": [_safe_document(item, include_url=True) for item in items], "next_cursor": next_cursor}


async def conversation_activity(org_id: str, conversation_id: str) -> dict:
    conversation = await db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id}, {"customer_id": 1, "platform": 1})
    if not conversation or conversation.get("platform") != "whatsapp":
        raise HTTPException(status_code=404, detail="WhatsApp conversation not found")
    phone = normalize_phone(conversation.get("customer_id"))
    if not phone:
        return {"checkout": None, "orders": []}
    checkout = await db.shopify_checkouts.find_one({"org_id": org_id, "phone": phone, "record_status": "abandoned"}, sort=[("updated_at", -1)])
    orders = await db.shopify_orders.find({"org_id": org_id, "phone": phone}).sort("created_at", -1).limit(3).to_list(length=3)
    return {"checkout": _safe_document(checkout, include_url=True), "orders": [_safe_document(item) for item in orders]}


def _product_summary(products: list[dict]) -> str:
    names = [re.sub(r"\s+", " ", str(item.get("name") or "Product")).strip()[:80] for item in products if item.get("name")]
    summary = ", ".join(names[:3])
    if len(names) > 3:
        summary += f" +{len(names) - 3} more"
    return summary[:240]


async def _live_consent(connection: dict, checkout: dict) -> tuple[str, str]:
    customer_id = checkout.get("customer_id")
    if not customer_id:
        return "", "UNKNOWN"
    data = await _graphql(connection, CUSTOMER_CONSENT_QUERY, {"id": customer_id})
    phone, consent, _ = _phone_and_consent(data.get("customer"))
    return phone, consent


async def _local_opted_out(org_id: str, phone: str) -> bool:
    conversation_id = f"whatsapp_{phone.lstrip('+')}"
    message = await db[f"messages_{org_id}"].find_one({"conversation_id": conversation_id, "role": "customer", "mode": {"$ne": "private"}}, {"content": 1}, sort=[("timestamp", -1)])
    command = re.sub(r"[^a-z]", "", str((message or {}).get("content") or "").lower())
    return command in {"stop", "unsubscribe", "cancel", "end", "quit"}


async def process_shopify_reminders() -> None:
    now = utcnow()
    await db.shopify_checkouts.update_many(
        {"reminder_status": "sending", "claim_expires_at": {"$lte": now}, "provider_started_at": {"$exists": False}},
        {"$set": {"reminder_status": "retryable", "reminder_due_at": now}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )
    await db.shopify_checkouts.update_many(
        {"reminder_status": "sending", "claim_expires_at": {"$lte": now}, "provider_started_at": {"$exists": True}},
        {"$set": {"reminder_status": "delivery_unknown", "failure_category": "provider_outcome_unknown", "reminder_due_at": None}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
    )
    for _ in range(10):
        token = secrets.token_urlsafe(16)
        now = utcnow()
        checkout = await db.shopify_checkouts.find_one_and_update(
            {"reminder_status": {"$in": ["pending", "retryable"]}, "reminder_due_at": {"$lte": now}, "$or": [{"claim_expires_at": {"$lte": now}}, {"claim_expires_at": None}]},
            {"$set": {"reminder_status": "sending", "claim_token": token, "claim_expires_at": now + timedelta(minutes=CLAIM_LEASE_MINUTES), "claimed_at": now}, "$inc": {"attempts": 1}},
            sort=[("reminder_due_at", 1)], return_document=ReturnDocument.AFTER,
        )
        if not checkout:
            break
        await _send_claimed_checkout(checkout, token)
    cursor = db.shopify_checkouts.find({"reminder_status": "provider_accepted"}).limit(10)
    async for checkout in cursor:
        await _reconcile_accepted(checkout)


async def _fail_claim(checkout: dict, token: str, status: str, reason: str, *, retry: bool = False) -> None:
    update: dict[str, Any] = {"reminder_status": "retryable" if retry else status, "failure_category": reason, "updated_at": utcnow()}
    if retry:
        update["reminder_due_at"] = utcnow() + timedelta(minutes=5)
    else:
        update["reminder_due_at"] = None
    await db.shopify_checkouts.update_one({"_id": checkout["_id"], "claim_token": token}, {"$set": update, "$unset": {"claim_token": "", "claim_expires_at": ""}})
    await _commerce_event(checkout["org_id"], "checkout", checkout["provider_id"])


async def _send_claimed_checkout(checkout: dict, token: str) -> None:
    org_id = checkout["org_id"]
    provider_started = False
    try:
        connection = await db.shopify_connections.find_one({"org_id": org_id, "status": "connected", "automation_enabled": True})
        if not connection:
            await _fail_claim(checkout, token, "cancelled", "automation_disabled")
            return
        latest = await db.shopify_checkouts.find_one({"org_id": org_id, "phone": checkout.get("phone"), "record_status": "abandoned"}, sort=[("updated_at", -1)]) if checkout.get("phone") else None
        if latest and latest["_id"] != checkout["_id"]:
            await _fail_claim(checkout, token, "superseded", "newer_checkout")
            return
        order_matches = []
        if checkout.get("checkout_token"):
            order_matches.append({"cart_token": checkout["checkout_token"]})
            order_matches.append({"checkout_token": checkout["checkout_token"]})
        order_match = {"org_id": org_id, "$or": order_matches or [{"provider_id": "__none__"}]}
        if checkout.get("completed_at") or await db.shopify_orders.find_one(order_match, {"_id": 1}):
            await _fail_claim(checkout, token, "converted", "order_created")
            return
        phone, consent = await _live_consent(connection, checkout)
        if not phone:
            await _fail_claim(checkout, token, "phone_missing", "phone_missing")
            return
        if phone != checkout.get("phone") or consent != "SUBSCRIBED" or await _local_opted_out(org_id, phone):
            await _fail_claim(checkout, token, "permission_missing", "consent_missing")
            return
        prior = await db.shopify_checkouts.find_one({"org_id": org_id, "phone": phone, "reminder_status": "sent", "sent_at": {"$gte": utcnow() - timedelta(hours=24)}}, {"_id": 1})
        if prior:
            await _fail_claim(checkout, token, "superseded", "phone_frequency_cap")
            return
        wa = await db.whatsapp_connections.find_one({"org_id": org_id, "wa_id": connection.get("whatsapp_id"), "is_active": {"$ne": False}}, {"wa_id": 1, "phone_id": 1, "access_token": 1})
        templates = await list_compatible_templates(org_id, connection.get("whatsapp_id"), raise_unavailable=True)
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
            db.shopify_checkouts.find_one({"_id": checkout["_id"], "claim_token": token, "reminder_status": "sending", "record_status": "abandoned", "completed_at": None}, {"_id": 1}),
            db.shopify_connections.find_one({"org_id": org_id, "status": "connected", "automation_enabled": True, "whatsapp_id": wa["wa_id"], "template_name": template["name"], "template_language": template["language"]}, {"_id": 1}),
            _local_opted_out(org_id, phone),
        )
        if not current:
            return
        if not current_connection:
            await _fail_claim(checkout, token, "cancelled", "configuration_changed")
            return
        if opted_out:
            await _fail_claim(checkout, token, "permission_missing", "local_opt_out")
            return
        started = await db.shopify_checkouts.find_one_and_update(
            {"_id": checkout["_id"], "claim_token": token, "reminder_status": "sending", "provider_started_at": {"$exists": False}},
            {"$set": {"provider_started_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        if not started:
            return
        if not await db.shopify_connections.find_one({"org_id": org_id, "status": "connected", "automation_enabled": True, "whatsapp_id": wa["wa_id"], "template_name": template["name"], "template_language": template["language"]}, {"_id": 1}):
            await _fail_claim(checkout, token, "cancelled", "configuration_changed")
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
                    {"$set": {"reminder_status": "provider_accepted", "provider_message_id": message_id, "provider_accepted_at": utcnow(), "updated_at": utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}},
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
        retry = exc.status_code in {502, 503, 504} and int(checkout.get("attempts") or 0) < 3
        await _fail_claim(checkout, token, "failed", "pre_provider_failure", retry=retry)
    except Exception as exc:
        logger.warning("Shopify reminder failed before completion: {}", type(exc).__name__)
        if provider_started:
            await _fail_claim(checkout, token, "delivery_unknown", "provider_outcome_unknown")
            return
        retry = int(checkout.get("attempts") or 0) < 3
        await _fail_claim(checkout, token, "failed", "pre_provider_failure", retry=retry)


async def _reconcile_accepted(checkout: dict) -> None:
    message_id = checkout.get("provider_message_id")
    if not message_id:
        return
    org_id = checkout["org_id"]
    phone = checkout.get("phone", "").lstrip("+")
    connection = await db.shopify_connections.find_one({"org_id": org_id}, {"whatsapp_id": 1})
    if not connection or not connection.get("whatsapp_id") or not phone:
        return
    if not sync_db[f"messages_{org_id}"].find_one({"message_id": message_id}, {"_id": 1}):
        name = str(checkout.get("customer_name") or "there").split(" ", 1)[0]
        content = f"Hi {name}, you left {_product_summary(checkout.get('products') or [])} in your cart. Complete your order here: {checkout.get('recovery_url') or ''}"
        conversation_id = await create_or_update_whatsapp_conversation(
            org_id=org_id, recipient_id=connection["whatsapp_id"], customer_phone_no=phone,
            customer_name=checkout.get("customer_name") or phone, last_message=content,
            last_sender="agent", is_auto_followup=True, extra_updates={"last_message_source": "shopify_abandoned_checkout"},
        )
        try:
            await store_whatsapp_message(
                org_id=org_id, content=content, conversation_id=conversation_id, customer_phone_no=phone,
                type="template", payload=None, recipient_id=connection["whatsapp_id"], role=MessageRole.AGENT,
                sender_name="Shopify recovery", message_id=message_id, status="sent",
                bot_context={"source": "shopify_abandoned_checkout", "checkout_id": checkout["provider_id"]},
            )
        except DuplicateKeyError:
            pass
    result = await db.shopify_checkouts.update_one(
        {"_id": checkout["_id"], "reminder_status": "provider_accepted", "provider_message_id": message_id},
        {"$set": {"reminder_status": "sent", "sent_at": checkout.get("provider_accepted_at") or utcnow(), "updated_at": utcnow()}},
    )
    if result.modified_count:
        await _commerce_event(org_id, "checkout", checkout["provider_id"])


async def _commerce_event(org_id: str, record_type: str, record_id: str) -> None:
    await broadcast_org_ws(org_id, "shopify_commerce_updated", {"org_id": org_id, "record_type": record_type, "record_id": record_id})


async def _admin_warning(org_id: str, category: str) -> None:
    if category == "reminder_configuration":
        await db.shopify_connections.update_one(
            {"org_id": org_id, "status": "connected"},
            {"$set": {"last_error": "Shopify recovery needs an active WhatsApp number and approved template", "updated_at": utcnow()}},
        )
    await broadcast_org_admin_ws(org_id, "shopify_sync_warning", {"org_id": org_id, "record_type": "warning", "record_id": category})
