"""Authenticated pre-checkout carts, using the existing recovery queue."""

import hashlib
import hmac
import json
import re
from datetime import timedelta
from urllib.parse import parse_qs

from fastapi import HTTPException


CUSTOMER_VARIANTS_QUERY = """
query StorefrontRecovery($customer: ID!, $variants: [ID!]!) {
  customer(id: $customer) { id firstName lastName defaultPhoneNumber { phoneNumber } }
  nodes(ids: $variants) {
    ... on ProductVariant { id title product { title status } }
  }
}
"""


def authenticated_customer(query: str, raw: bytes, secret: str, now: float) -> tuple[str, str]:
    """Shopify signs query values; body_sha256 binds the forwarded POST body too."""
    try:
        values = parse_qs(query, keep_blank_values=True, max_num_fields=30)
    except ValueError:
        raise HTTPException(401, "Invalid Shopify app proxy query") from None
    signatures = values.pop("signature", [])
    canonical = "".join(sorted(f"{key}={','.join(items)}" for key, items in values.items()))
    expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    if (not secret or len(signatures) != 1 or not re.fullmatch(r"[a-f0-9]{64}", signatures[0])
            or not hmac.compare_digest(signatures[0], expected)):
        raise HTTPException(401, "Invalid Shopify app proxy signature")
    for key in ("shop", "logged_in_customer_id", "timestamp", "body_sha256"):
        if len(values.get(key, [])) != 1:
            raise HTTPException(401, "Invalid Shopify app proxy parameters")
    try:
        stamp = int(values["timestamp"][0])
    except ValueError:
        raise HTTPException(401, "Invalid Shopify app proxy timestamp") from None
    if abs(now - stamp) > 120:
        raise HTTPException(401, "Expired Shopify app proxy request")
    if (not re.fullmatch(r"[a-f0-9]{64}", values["body_sha256"][0])
            or not hmac.compare_digest(values["body_sha256"][0], hashlib.sha256(raw).hexdigest())):
        raise HTTPException(401, "Cart payload signature mismatch")
    shop, customer = values["shop"][0], values["logged_in_customer_id"][0]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", shop):
        raise HTTPException(401, "Invalid Shopify shop")
    if not re.fullmatch(r"[1-9][0-9]{0,24}", customer):
        raise HTTPException(401, "Sign in to your store account first")
    return shop, f"gid://shopify/Customer/{customer}"


def cart_payload(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "Invalid cart payload") from None
    if not isinstance(data, dict) or set(data) - {"token", "items", "event", "consent"}:
        raise HTTPException(422, "Unsupported cart fields")
    token = data.get("token")
    # The Ajax cart token's ?key= suffix is a private capability, never store it.
    token = token.split("?", 1)[0] if isinstance(token, str) else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", token):
        raise HTTPException(422, "Invalid cart token")
    event = data.get("event", "updated")
    if not isinstance(event, str) or event not in {"updated", "checkout_started", "consent_revoked", "consent_granted"}:
        raise HTTPException(422, "Invalid cart event")
    items = data.get("items", [])
    if not isinstance(items, list) or len(items) > 50:
        raise HTTPException(422, "Cart must contain at most 50 variants")
    quantities = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "quantity"}:
            raise HTTPException(422, "Invalid cart item")
        ident, qty = str(item["id"]), item["quantity"]
        if not re.fullmatch(r"[1-9][0-9]{0,24}", ident) or type(qty) is not int or not 1 <= qty <= 100:
            raise HTTPException(422, "Invalid cart variant or quantity")
        quantities[ident] = quantities.get(ident, 0) + qty
        if quantities[ident] > 100:
            raise HTTPException(422, "Cart quantity exceeds limit")
    if type(data.get("consent", False)) is not bool:
        raise HTTPException(422, "Invalid consent value")
    return {"token": token, "event": event, "consent": data.get("consent", False),
            "items": [{"id": key, "quantity": quantities[key]} for key in sorted(quantities)]}


async def capture_cart(query: str, raw: bytes) -> dict:
    from services import shopify_service as s

    now = s.utcnow()
    shop, customer_id = authenticated_customer(query, raw, s.SHOPIFY_CLIENT_SECRET, now.timestamp())
    payload = cart_payload(raw)
    connection = await s.db.shopify_connections.find_one({"shop": shop, "status": "connected"})
    if not connection:
        return {"status": "disabled"}
    consent_key = {"_id": f"{connection['org_id']}:{customer_id}"}
    if payload["event"] == "consent_revoked":
        await s.db.shopify_storefront_consents.update_one(consent_key, {"$set": {
            "org_id": connection["org_id"], "customer_id": customer_id,
            "granted": False, "updated_at": now,
        }}, upsert=True)
        await s.db.shopify_checkouts.update_many({
            "org_id": connection["org_id"], "customer_id": customer_id, "source": "storefront_cart",
            "provider_started_at": {"$exists": False},
        }, {"$set": {"storefront_consent": False, "record_status": "deleted",
                     "reminder_status": "opted_out", "reminder_due_at": None, "status_updated_at": now},
            "$unset": {"claim_token": "", "claim_expires_at": ""}})
        return {"status": "opted_out"}
    if not connection.get("automation_enabled"):
        return {"status": "disabled"}
    if payload["event"] == "consent_granted" and payload["consent"]:
        await s.db.shopify_storefront_consents.update_one(consent_key, {"$set": {
            "org_id": connection["org_id"], "customer_id": customer_id,
            "granted": True, "updated_at": now, "source": "storefront_whatsapp_cart_checkbox_v1",
        }}, upsert=True)
    preference = await s.db.shopify_storefront_consents.find_one(consent_key)
    if not payload["consent"] or not preference or not preference.get("granted"):
        return {"status": "consent_required"}
    cart_key = hashlib.sha256(payload["token"].encode()).hexdigest()[:32]
    key = {"org_id": connection["org_id"], "provider_id": f"storefront:{customer_id.rsplit('/', 1)[-1]}:{cart_key}"}
    existing = await s.db.shopify_checkouts.find_one(key)
    if existing and existing.get("checkout_token") == payload["token"] and (
        s._delivery_started(existing) or existing.get("storefront_checkout_started")
    ):
        return {"status": "already_processed"}
    # Rate-limit customer API reads, and make retransmission a no-op so the
    # five-minute abandonment clock is not reset by polling or page navigation.
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if existing and existing.get("cart_fingerprint") == fingerprint:
        return {"status": existing.get("reminder_status", "pending")}
    latest = await s.db.shopify_checkouts.find_one(
        {"org_id": connection["org_id"], "customer_id": customer_id, "source": "storefront_cart"},
        sort=[("provider_updated_at", -1)],
    )
    if latest and payload["event"] in {"updated", "consent_granted"} and payload["items"] and (
        now - s._parse_datetime(latest["provider_updated_at"]) < timedelta(seconds=5)
    ):
        raise HTTPException(429, "Cart changed too quickly; try again shortly")
    document = {
        "shop": shop, "customer_id": customer_id, "source": "storefront_cart",
        "checkout_token": payload["token"], "cart_fingerprint": fingerprint,
        "storefront_items": payload["items"], "storefront_consent": True,
        "consent_source": "storefront_whatsapp_cart_checkbox_v1", "consent_updated_at": now,
        "storefront_checkout_started": payload["event"] == "checkout_started",
        "products": [], "completed_at": None,
        "record_status": "deleted", "reminder_status": "cancelled", "reminder_due_at": None,
        "provider_updated_at": now, "updated_at": now, "status_updated_at": now,
    }
    if payload["event"] in {"updated", "consent_granted"} and payload["items"]:
        data = await s._graphql(connection, CUSTOMER_VARIANTS_QUERY, {
            "customer": customer_id,
            "variants": [f"gid://shopify/ProductVariant/{item['id']}" for item in payload["items"]],
        })
        customer = data.get("customer") or {}
        if customer.get("id") != customer_id:
            raise HTTPException(422, "Shopify customer could not be verified")
        phone = s.normalize_phone((customer.get("defaultPhoneNumber") or {}).get("phoneNumber"))
        await s.db.shopify_storefront_consents.update_one(consent_key, {"$set": {"phone_key": s._phone_key(phone)}})
        variants = {node["id"]: node for node in data.get("nodes", []) if node}
        products = []
        for item in payload["items"]:
            node = variants.get(f"gid://shopify/ProductVariant/{item['id']}")
            if not node or (node.get("product") or {}).get("status") != "ACTIVE":
                raise HTTPException(422, "Cart contains a product that is no longer available")
            title = node["product"]["title"]
            if node.get("title") and node["title"] != "Default Title":
                title += f" - {node['title']}"
            products.append({"id": node["id"], "name": title[:300], "quantity": item["quantity"]})
        document.update({"phone": phone, "storefront_consent_phone": phone, "customer_name": s._name(customer), "products": products,
            "record_status": "abandoned", "reminder_status": "pending" if phone else "phone_missing",
            "reminder_due_at": now + timedelta(minutes=5) if phone else None,
            "recovery_url": f"https://{shop}/cart/" + ",".join(f"{item['id']}:{item['quantity']}" for item in payload["items"]) + "?storefront=true",
        })
    if existing:
        # Serialize against a concurrently claimed send and concurrent captures.
        if existing.get("provider_started_at"):
            return {"status": "already_processed"}
        result = await s.db.shopify_checkouts.update_one({**key,
            "provider_updated_at": existing["provider_updated_at"], "provider_started_at": {"$exists": False}},
            {"$set": document, "$unset": {"claim_token": "", "claim_expires_at": "", "failure_category": ""}})
        if not result.matched_count:
            raise HTTPException(409, "Cart changed; retry with the current cart")
    else:
        try:
            await s.db.shopify_checkouts.insert_one({**key, **document, "created_at": now, "provider_created_at": now})
        except s.DuplicateKeyError:
            raise HTTPException(409, "Cart changed; retry with the current cart") from None
    if document.get("phone"):
        await s._reconcile_phone_checkouts(connection["org_id"], document["phone"], connection)
    return {"status": document["reminder_status"]}


async def refresh_cart_before_send(checkout: dict, token: str, connection: dict) -> dict | None:
    from services import shopify_service as s

    current = await s.db.shopify_checkouts.find_one({"_id": checkout["_id"], "claim_token": token})
    if not current:
        return None
    preference = await s.db.shopify_storefront_consents.find_one({"_id": f"{checkout['org_id']}:{checkout['customer_id']}"})
    if (not preference or not preference.get("granted") or not current.get("storefront_consent") or current.get("storefront_checkout_started")
            or current.get("record_status") != "abandoned"):
        await s._fail_claim(current, token, "cancelled", "cart_no_longer_eligible")
        return None
    due = s._parse_datetime(current["provider_updated_at"]) + timedelta(minutes=5)
    if due > s.utcnow():
        await s._defer_claim(current, token, due, "cart_changed")
        return None
    # Shopify's authoritative customer phone is rechecked at send time. A changed
    # phone needs a fresh storefront consent capture, never silently retarget it.
    data = await s._graphql(connection, CUSTOMER_VARIANTS_QUERY, {
        "customer": current["customer_id"],
        "variants": [f"gid://shopify/ProductVariant/{item['id']}" for item in current["storefront_items"]],
    })
    customer = data.get("customer") or {}
    phone = s.normalize_phone((customer.get("defaultPhoneNumber") or {}).get("phoneNumber"))
    nodes = [node for node in data.get("nodes", []) if node]
    if (customer.get("id") != current["customer_id"] or not phone
            or phone != current.get("phone") or phone != current.get("storefront_consent_phone")):
        await s._fail_claim(current, token, "cancelled", "customer_phone_changed")
        return None
    if len(nodes) != len(current["storefront_items"]) or any((node.get("product") or {}).get("status") != "ACTIVE" for node in nodes):
        await s._fail_claim(current, token, "cancelled", "cart_product_unavailable")
        return None
    return current
