"""Pure connection rules shared by WhatsApp authentication and messaging."""

from __future__ import annotations

import re
from typing import Iterable, Optional


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
MAX_WHATSAPP_CONNECTIONS = 50


def whatsapp_connection_limit(_org_id: str) -> int:
    """Every organization can connect up to 50 WhatsApp numbers."""
    return MAX_WHATSAPP_CONNECTIONS


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
    """Validate same-WABA/capacity rules and return a stable connection slot."""
    connections = [row for row in existing_connections if row.get("is_active") is not False]
    existing = next(
        (row for row in connections if str(row.get("phone_id") or row.get("phone_number_id") or "") == phone_number_id),
        None,
    )
    if any(str(row.get("wa_id") or row.get("waba_id") or "") != waba_id for row in connections):
        raise ValueError("All WhatsApp numbers in an organization must belong to the same WABA")
    if not existing and len(connections) >= limit:
        raise ValueError(f"This organization can connect at most {limit} WhatsApp number{'s' if limit != 1 else ''}")

    used_slots = set()
    existing_slot = None
    for row in connections:
        slot = row.get("slot")
        if not isinstance(slot, int) or not 1 <= slot <= limit or slot in used_slots:
            slot = next(candidate for candidate in range(1, limit + 1) if candidate not in used_slots)
        used_slots.add(slot)
        if row is existing:
            existing_slot = slot
    if existing:
        return existing_slot, existing
    for slot in range(1, limit + 1):
        if slot not in used_slots:
            return slot, existing
    raise ValueError("No WhatsApp connection slot is available")


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
    slot = connection.get("slot") or connection.get("last_slot") or 1
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
    }


def public_connections(connections: Iterable[dict]) -> list[dict]:
    """Repair duplicate legacy slots in the API response without exposing secrets."""
    used_slots: set[int] = set()
    result = []
    for connection in connections:
        slot = connection.get("slot")
        if not isinstance(slot, int) or slot < 1 or slot in used_slots:
            slot = 1
            while slot in used_slots:
                slot += 1
        used_slots.add(slot)
        result.append(public_connection({**connection, "slot": slot}))
    return result


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
