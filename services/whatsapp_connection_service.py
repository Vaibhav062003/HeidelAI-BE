"""Pure connection rules shared by WhatsApp authentication and messaging."""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Optional


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_ORG_ID = re.compile(r"^org_[A-Za-z0-9]+$")
MAX_WHATSAPP_CONNECTIONS = 50
logger = logging.getLogger(__name__)


def _parse_org_number_limits(raw: str) -> dict[str, int]:
    limits: dict[str, int] = {}
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            logger.warning("Ignoring malformed WHATSAPP_ORG_NUMBER_LIMITS entry")
            continue
        org_id, value = (part.strip() for part in item.split("=", 1))
        if not _ORG_ID.fullmatch(org_id):
            logger.warning("Ignoring WHATSAPP_ORG_NUMBER_LIMITS entry with invalid organization id")
            continue
        if org_id in seen:
            limits.pop(org_id, None)
            duplicates.add(org_id)
            logger.warning("Ignoring duplicate WhatsApp number limit for org %s", org_id)
            continue
        seen.add(org_id)
        try:
            limit = int(value)
        except ValueError:
            logger.warning("Ignoring invalid WhatsApp number limit for org %s", org_id)
            continue
        if not 1 <= limit <= MAX_WHATSAPP_CONNECTIONS:
            logger.warning("Ignoring out-of-range WhatsApp number limit for org %s", org_id)
            continue
        limits[org_id] = limit
    for org_id in duplicates:
        limits.pop(org_id, None)
    return limits


WHATSAPP_ORG_NUMBER_LIMITS = _parse_org_number_limits(os.getenv("WHATSAPP_ORG_NUMBER_LIMITS", ""))


def whatsapp_connection_limit(org_id: str) -> int:
    """Return the configured entitlement; unlisted organizations get one number."""
    return WHATSAPP_ORG_NUMBER_LIMITS.get(str(org_id), 1)


def normalize_connection_label(label: Optional[str], slot: int) -> str:
    value = str(label or "").strip()
    if not value:
        return f"WhatsApp {slot}"
    if len(value) > 24 or _CONTROL_CHARACTERS.search(value):
        raise ValueError("WhatsApp connection labels must be 1 to 24 printable characters")
    return value


def connection_label_key(label: str) -> str:
    return " ".join(str(label or "").split()).casefold()


def select_exact_phone_number(phone_rows: object, phone_number_id: str) -> dict:
    """Select only the phone id supplied by Meta Embedded Signup."""
    expected = str(phone_number_id or "").strip()
    if not expected:
        raise ValueError("phone_number_id is required")
    if not isinstance(phone_rows, list):
        raise ValueError("Meta did not return a phone-number list")
    for row in phone_rows:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == expected:
            return row
    raise ValueError("The selected phone number was not returned for this WhatsApp Business Account")


def choose_connection_slot(
    existing_connections: Iterable[dict],
    *,
    phone_number_id: str,
    waba_id: str,
    limit: int,
) -> tuple[int, Optional[dict]]:
    """Validate capacity rules and return a stable connection slot."""
    connections = [row for row in existing_connections if row.get("is_active") is not False]
    validate_active_connection_slots(connections)
    existing = next(
        (row for row in connections if str(row.get("phone_id") or row.get("phone_number_id") or "") == phone_number_id),
        None,
    )
    if not existing and len(connections) >= limit:
        raise ValueError(f"This organization can connect at most {limit} WhatsApp number{'s' if limit != 1 else ''}")

    used_slots = {row["slot"] for row in connections}
    if existing:
        return existing["slot"], existing
    for slot in range(1, limit + 1):
        if slot not in used_slots:
            return slot, existing
    raise ValueError("No WhatsApp connection slot is available")


def validate_active_connection_slots(
    connections: Iterable[dict],
    *,
    limit: int = MAX_WHATSAPP_CONNECTIONS,
) -> list[dict]:
    """Return active connections in slot order or reject corrupt persisted state."""
    rows = [row for row in connections if row.get("is_active") is not False]
    if len(rows) > limit:
        raise ValueError(f"An organization can have at most {limit} active WhatsApp numbers")
    phone_ids = [str(row.get("phone_id") or row.get("phone_number_id") or "").strip() for row in rows]
    if any(not value for value in phone_ids) or len(phone_ids) != len(set(phone_ids)):
        raise ValueError("Active WhatsApp phone identifiers must be present and unique")
    slots = [row.get("slot") for row in rows]
    if not all(isinstance(slot, int) for slot in slots) or sorted(slots) != list(range(1, len(rows) + 1)):
        raise ValueError("Active WhatsApp slots must be complete, unique, and contiguous")
    return sorted(rows, key=lambda row: row["slot"])


def whatsapp_conversation_id(customer_phone: str, business_phone_id: Optional[str] = None) -> str:
    """Build a sender-aware id while retaining the legacy id when metadata is absent."""
    customer = str(customer_phone or "").strip()
    phone_id = str(business_phone_id or "").strip()
    return f"whatsapp_{phone_id}_{customer}" if phone_id else f"whatsapp_{customer}"


def whatsapp_conversation_scope(phone_id: str, include_legacy: bool = False) -> dict:
    """Scope conversations to one line, including pre-migration rows for the primary line."""
    exact = {"business_phone_id": str(phone_id)}
    if not include_legacy:
        return {"platform": "whatsapp", **exact}
    return {
        "platform": "whatsapp",
        "$or": [exact, {"business_phone_id": {"$exists": False}}, {"business_phone_id": None}],
    }


def public_connection(connection: dict) -> dict:
    """Return the non-secret connection contract used by settings clients."""
    phone_id = str(connection.get("phone_id") or connection.get("phone_number_id") or "")
    wa_id = str(connection.get("wa_id") or connection.get("waba_id") or "")
    user_id = str(connection.get("user_id") or phone_id)
    is_active = connection.get("is_active") is not False
    slot = connection.get("slot") if is_active else connection.get("last_slot")
    return {
        "connection_id": user_id,
        "user_id": user_id,
        "waba_id": wa_id,
        "wa_id": wa_id,
        "phone_number_id": phone_id,
        "phone_id": phone_id,
        "phone": connection.get("phone") or connection.get("display_phone_number"),
        "name": connection.get("name"),
        "label": connection.get("label"),
        "slot": slot,
        "is_primary": is_active and slot == 1,
        "wa_bot_flow_id": connection.get("wa_bot_flow_id"),
        "business_info": connection.get("business_info") or {},
        "connected_at": connection.get("connected_at"),
        "is_active": is_active,
        "disconnected_at": connection.get("disconnected_at"),
        "provider_revoke_status": connection.get("provider_revoke_status"),
        "webhook_status": connection.get("webhook_status"),
        "last_webhook_at": connection.get("last_webhook_at"),
        "webhook_error": connection.get("webhook_error"),
    }


def public_connections(connections: Iterable[dict]) -> list[dict]:
    """Return active connections without masking persisted slot corruption."""
    return [public_connection(connection) for connection in validate_active_connection_slots(connections)]


def public_historical_connections(connections: Iterable[dict]) -> list[dict]:
    """Return one non-secret historical row per disconnected phone number."""
    result = []
    seen: set[str] = set()
    for connection in connections:
        phone_id = str(connection.get("phone_id") or connection.get("phone_number_id") or "")
        if not phone_id or phone_id in seen:
            continue
        seen.add(phone_id)
        result.append(public_connection(connection))
    return result
