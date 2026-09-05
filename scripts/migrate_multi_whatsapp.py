"""Validate and backfill the multi-number WhatsApp model.

Dry-run is the default. Apply only after the report has no errors:

    python -m scripts.migrate_multi_whatsapp
    python -m scripts.migrate_multi_whatsapp --apply

Repair a completed migration whose legacy conversations were assigned to the
wrong primary line:

    python -m scripts.migrate_multi_whatsapp --org-id ORG --repair-primary-phone-id PHONE
    python -m scripts.migrate_multi_whatsapp --org-id ORG --repair-primary-phone-id PHONE --apply
"""

import argparse
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from pymongo import ReturnDocument
    from pymongo.errors import DuplicateKeyError
except ModuleNotFoundError:  # Keeps dependency-free planning/tests usable.
    class ReturnDocument:
        AFTER = True

    class DuplicateKeyError(Exception):
        pass

from services.whatsapp_connection_service import MAX_WHATSAPP_CONNECTIONS

MIGRATION_ID = "multi_whatsapp_v2"
LEASE_SECONDS = 3600
DEFAULT_ORG_BATCH_SIZE = 25
DEFAULT_MAX_ROWS_PER_ORG = 1000
DEFAULT_REPORT_ITEMS = 100
DEFAULT_MAX_CONVERSATIONS = 100_000


def _connected_at(connection: dict) -> str:
    value = connection.get("connected_at")
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def _connection_order(connection: dict) -> tuple[str, str]:
    return _connected_at(connection), str(connection.get("_id") or "")


def _label(connection: dict, slot: int, used: set[str]) -> tuple[str, str]:
    candidate = " ".join(str(connection.get("label") or connection.get("name") or "").split())[:24]
    key = candidate.casefold()
    if not candidate or key in used:
        base = f"WhatsApp {slot}"
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base} {suffix}"
            suffix += 1
        key = candidate.casefold()
    used.add(key)
    return candidate, key


def merge_conversation_documents(documents: list[dict], business_phone_id: str) -> dict:
    """Keep the newest conversation state while preserving older non-null fields."""
    ordered = sorted(
        documents,
        key=lambda row: (
            str(row.get("last_message_timestamp") or row.get("updated_at") or row.get("created_at") or ""),
            str(row.get("_id") or ""),
        ),
    )
    if len(ordered) < 2:
        raise ValueError("At least two conversation documents are required")
    survivor = ordered[-1]
    merged = {}
    for document in ordered:
        merged.update({key: value for key, value in document.items() if value is not None})
    merged["business_phone_id"] = str(business_phone_id)
    merged["customer_id"] = str(survivor.get("customer_id") or "")
    for field in ("created_at", "started_at"):
        values = [document[field] for document in ordered if document.get(field) is not None]
        if values:
            merged[field] = min(values, key=str)
    for field in ("message_counter", "unread_count"):
        values = [document.get(field) for document in ordered if isinstance(document.get(field), int)]
        if values:
            merged[field] = max(values)
    return {
        "survivor_id": survivor["_id"],
        "duplicate_ids": [document["_id"] for document in ordered[:-1]],
        "conversation_ids": sorted({
            str(document.get("conversation_id")) for document in ordered if document.get("conversation_id")
        }),
        "document": {key: value for key, value in merged.items() if key != "_id"},
    }


def _duplicate_conversation_groups(conversations, primary_phone_id: str):
    return conversations.aggregate([
        {"$match": {"platform": "whatsapp", "customer_id": {"$type": "string"}}},
        {"$group": {
            "_id": {
                "business_phone_id": {"$ifNull": ["$business_phone_id", primary_phone_id]},
                "customer_id": "$customer_id",
            },
            "document_ids": {"$push": "$_id"},
            "count": {"$sum": 1},
        }},
        {"$match": {"count": {"$gt": 1}}},
    ], allowDiskUse=True)


def _merge_duplicate_conversations(db, org_id: str, primary_phone_id: str, now: datetime) -> int:
    conversations = db[f"conversations_{org_id}"]
    contacts = db[f"contacts_{org_id}"]
    archive = db[f"archived_duplicate_conversations_{org_id}"]
    merged_count = 0
    for group in _duplicate_conversation_groups(conversations, primary_phone_id):
        documents = list(conversations.find({"_id": {"$in": group["document_ids"]}}))
        if len(documents) < 2:
            continue
        merge = merge_conversation_documents(documents, group["_id"]["business_phone_id"])
        canonical_id = str(merge["document"].get("conversation_id") or "")
        old_ids = [value for value in merge["conversation_ids"] if value != canonical_id]
        for document in documents:
            if document["_id"] in merge["duplicate_ids"]:
                archive.replace_one(
                    {"_id": document["_id"]},
                    {**document, "migration_archived_at": now, "merged_into": canonical_id},
                    upsert=True,
                )
        if old_ids:
            for collection_name in (f"messages_{org_id}", f"private_notes_{org_id}", f"escalations_{org_id}"):
                db[collection_name].update_many(
                    {"conversation_id": {"$in": old_ids}},
                    {"$set": {"conversation_id": canonical_id}},
                )
            for collection_name in (
                "scheduled_messages", "automation_scheduled_followups",
                "whatsapp_calls", "whatsapp_outbound_sends",
            ):
                db[collection_name].update_many(
                    {"org_id": org_id, "conversation_id": {"$in": old_ids}},
                    {"$set": {"conversation_id": canonical_id}},
                )
            contacts.update_many(
                {"conversation_id": {"$in": old_ids}},
                {"$set": {"conversation_id": canonical_id}},
            )
            contacts.update_many(
                {"conversation_ids": {"$in": old_ids}},
                {"$addToSet": {"conversation_ids": canonical_id}},
            )
            contacts.update_many(
                {"conversation_ids": {"$in": old_ids}},
                {"$pull": {"conversation_ids": {"$in": old_ids}}},
            )
        conversations.update_one({"_id": merge["survivor_id"]}, {"$set": merge["document"]})
        conversations.delete_many({"_id": {"$in": merge["duplicate_ids"]}})
        merged_count += len(merge["duplicate_ids"])
    return merged_count


def organization_waba_errors(organizations: list[dict]) -> list[str]:
    owners = defaultdict(list)
    for organization in organizations:
        wa_id = str(organization.get("wa_id") or "").strip()
        if wa_id:
            owners[wa_id].append(str(organization.get("org_id") or organization.get("_id")))
    return [
        f"WABA {wa_id} is assigned to multiple organizations: {sorted(org_ids)}"
        for wa_id, org_ids in owners.items() if len(org_ids) > 1
    ]


def build_plan(connections: list[dict], primary_wa_ids: dict[str, str] | None = None) -> dict:
    """Return deterministic actions and errors without touching MongoDB."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    phone_owners: dict[str, set[str]] = defaultdict(set)
    waba_owners: dict[str, set[str]] = defaultdict(set)
    errors: list[str] = []

    for connection in connections:
        org_id = str(connection.get("org_id") or "")
        phone_id = str(connection.get("phone_id") or connection.get("phone_number_id") or "")
        wa_id = str(connection.get("wa_id") or connection.get("waba_id") or "")
        if not org_id or not phone_id or not wa_id:
            if connection.get("is_active") is False:
                continue
            errors.append(f"Connection {connection.get('_id')} is missing org_id, phone_id, or wa_id")
            continue
        grouped[org_id].append({**connection, "phone_id": phone_id, "wa_id": wa_id})
        if connection.get("is_active") is not False:
            phone_owners[phone_id].add(org_id)
            waba_owners[wa_id].add(org_id)

    for phone_id, owners in phone_owners.items():
        if len(owners) > 1:
            errors.append(f"Phone {phone_id} belongs to multiple organizations: {sorted(owners)}")
    for wa_id, owners in waba_owners.items():
        if len(owners) > 1:
            errors.append(f"WABA {wa_id} belongs to multiple organizations: {sorted(owners)}")

    organizations: dict[str, dict] = {}
    for org_id, org_connections in grouped.items():
        by_phone: dict[str, list[dict]] = defaultdict(list)
        for connection in org_connections:
            by_phone[str(connection["phone_id"])].append(connection)

        distinct = []
        duplicate_ids = []
        for same_phone in by_phone.values():
            active = [item for item in same_phone if item.get("is_active") is not False]
            if not active:
                duplicate_ids.extend(item["_id"] for item in same_phone)
                continue
            ordered = sorted(active, key=_connection_order, reverse=True)
            distinct.append(ordered[0])
            duplicate_ids.extend(item["_id"] for item in ordered[1:])
            duplicate_ids.extend(item["_id"] for item in same_phone if item.get("is_active") is False)

        if len(distinct) > MAX_WHATSAPP_CONNECTIONS:
            errors.append(f"Organization {org_id} has {len(distinct)} distinct WhatsApp numbers")
        preferred_phone_id = ""
        legacy_wa_id = str((primary_wa_ids or {}).get(org_id) or "")
        legacy_matches = [item for item in distinct if str(item["wa_id"]) == legacy_wa_id]
        explicit_primary = [item for item in distinct if item.get("slot") == 1]
        if len(legacy_matches) == 1:
            preferred_phone_id = str(legacy_matches[0]["phone_id"])
        elif len(legacy_matches) > 1 and len(explicit_primary) == 1 and explicit_primary[0] in legacy_matches:
            preferred_phone_id = str(explicit_primary[0]["phone_id"])
        elif len(legacy_matches) > 1:
            errors.append(f"Organization {org_id} primary WABA maps to multiple phone numbers")
        elif len(explicit_primary) == 1:
            preferred_phone_id = str(explicit_primary[0]["phone_id"])

        used_labels: set[str] = set()
        updates = []
        ordered = sorted(
            distinct,
            key=lambda item: (str(item["phone_id"]) != preferred_phone_id, _connection_order(item)),
        )
        for slot, connection in enumerate(ordered, start=1):
            label, label_key = _label(connection, slot, used_labels)
            updates.append({
                "_id": connection["_id"],
                "phone_id": str(connection["phone_id"]),
                "phone_number_id": str(connection["phone_id"]),
                "phone": connection.get("phone"),
                "wa_id": str(connection["wa_id"]),
                "waba_id": str(connection["wa_id"]),
                "slot": slot,
                "label": label,
                "label_key": label_key,
            })
        organizations[org_id] = {"updates": updates, "delete_ids": duplicate_ids}

    return {"errors": sorted(set(errors)), "organizations": organizations}


def conversation_mapping_plan(
    documents: list[dict],
    connections: list[dict],
    primary_phone_id: str,
    *,
    remap_unidentified_to_primary: bool = False,
) -> dict:
    """Resolve legacy conversations from their stored WABA without guessing."""
    by_phone = {
        str(item.get("phone_id") or item.get("phone_number_id") or ""): item
        for item in connections if item.get("is_active") is not False
    }
    identifier_owners: dict[str, set[str]] = defaultdict(set)
    for phone_id, connection in by_phone.items():
        for value in (
            phone_id,
            connection.get("wa_id"),
            connection.get("waba_id"),
        ):
            normalized = str(value or "").strip()
            if normalized:
                identifier_owners[normalized].add(phone_id)

    assignments: dict[str, list] = defaultdict(list)
    final_keys: dict[tuple[str, str], object] = {}
    collisions = 0
    unresolved = 0
    for document in documents:
        current = str(document.get("business_phone_id") or "").strip()
        source = str(document.get("whatsapp_id") or "").strip()
        owners = identifier_owners.get(source, set()) if source else set()
        target = next(iter(owners)) if len(owners) == 1 else ""
        if source and len(owners) > 1 and current in owners:
            target = current
        if not target and not source and (remap_unidentified_to_primary or current not in by_phone):
            target = primary_phone_id
        if not target and not source and current in by_phone:
            target = current
        if not target:
            unresolved += 1
            continue

        customer_id = document.get("customer_id")
        if isinstance(customer_id, str) and customer_id:
            key = (target, customer_id)
            if key in final_keys and final_keys[key] != document.get("_id"):
                collisions += 1
            else:
                final_keys[key] = document.get("_id")
        expected_phone = by_phone[target].get("phone")
        if current != target or document.get("business_phone") != expected_phone:
            assignments[target].append(document.get("_id"))

    return {
        "assignments": dict(assignments),
        "reassignments_by_phone_id": {
            phone_id: len(document_ids) for phone_id, document_ids in assignments.items()
        },
        "reassignment_count": sum(len(values) for values in assignments.values()),
        "collision_count": collisions,
        "unresolved_count": unresolved,
    }


def _conversation_documents(db, org_id: str, maximum: int) -> list[dict]:
    if maximum < 1:
        raise ValueError("The conversation limit must be positive")
    documents = list(db[f"conversations_{org_id}"].find(
        {"platform": "whatsapp"},
        {
            "_id": 1,
            "customer_id": 1,
            "whatsapp_id": 1,
            "business_phone_id": 1,
            "business_phone": 1,
        },
    ).limit(maximum + 1))
    if len(documents) > maximum:
        raise ValueError(f"Organization {org_id} exceeds the bounded {maximum}-conversation repair limit")
    return documents


def _apply_conversation_mapping(db, org_id: str, plan: dict, connections: list[dict]) -> None:
    conversations = db[f"conversations_{org_id}"]
    by_phone = {
        str(item.get("phone_id") or item.get("phone_number_id") or ""): item
        for item in connections
    }
    for phone_id, document_ids in plan["assignments"].items():
        connection = by_phone[phone_id]
        for offset in range(0, len(document_ids), 1000):
            conversations.update_many(
                {"_id": {"$in": document_ids[offset:offset + 1000]}},
                {"$set": {"business_phone_id": phone_id, "business_phone": connection.get("phone")}},
            )


def _org_checkpoint_id(org_id: str) -> str:
    return f"{MIGRATION_ID}:{org_id}"


def claim_org_lease(db, org_id: str, owner: str, now: datetime, lease_seconds: int = LEASE_SECONDS) -> bool:
    """Atomically claim one organization, without stealing a live or completed lease."""
    try:
        claimed = db.system_migrations.find_one_and_update(
            {
                "_id": _org_checkpoint_id(org_id),
                "status": {"$nin": ["data_complete", "complete"]},
                "$or": [
                    {"lease_until": {"$lt": now}},
                    {"lease_until": {"$exists": False}},
                    {"lease_owner": owner},
                ],
            },
            {"$set": {
                "migration_id": MIGRATION_ID,
                "org_id": org_id,
                "status": "running",
                "lease_owner": owner,
                "lease_until": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return False
    return bool(claimed and claimed.get("lease_owner") == owner)


def checkpoint_org(db, org_id: str, owner: str, status: str, now: datetime, **fields) -> bool:
    update = {"status": status, "updated_at": now, **fields}
    result = db.system_migrations.update_one(
        {"_id": _org_checkpoint_id(org_id), "lease_owner": owner},
        {"$set": update, "$unset": {"lease_owner": "", "lease_until": ""}},
    )
    return bool(result.modified_count)


def renew_org_lease(db, org_id: str, owner: str, now: datetime, lease_seconds: int = LEASE_SECONDS) -> bool:
    result = db.system_migrations.update_one(
        {"_id": _org_checkpoint_id(org_id), "status": "running", "lease_owner": owner},
        {"$set": {
            "lease_until": now + timedelta(seconds=lease_seconds),
            "updated_at": now,
        }},
    )
    return bool(result.modified_count)


def bounded_report(report: dict, max_items: int = DEFAULT_REPORT_ITEMS) -> dict:
    """Limit CLI output while keeping counts and resumability visible."""
    maximum = max(1, int(max_items))
    output = {key: value for key, value in report.items() if key != "organizations"}
    errors = list(output.get("errors") or [])
    output["error_count"] = len(errors)
    output["errors"] = errors[:maximum]
    organizations = {}
    for org_id, actions in list((report.get("organizations") or {}).items())[:maximum]:
        organizations[org_id] = {
            **actions,
            "updates": list(actions.get("updates") or [])[:maximum],
            "delete_ids": list(actions.get("delete_ids") or [])[:maximum],
        }
    output["organizations"] = organizations
    output["output_truncated"] = (
        len(errors) > maximum
        or len(report.get("organizations") or {}) > maximum
        or any(
            len(actions.get("updates") or []) > maximum or len(actions.get("delete_ids") or []) > maximum
            for actions in (report.get("organizations") or {}).values()
        )
    )
    return output


def _cross_org_ownership_errors(db, max_items: int) -> list[str]:
    errors = []
    for label, canonical in (
        ("Phone", {"$ifNull": ["$phone_id", "$phone_number_id"]}),
        ("WABA", {"$ifNull": ["$wa_id", "$waba_id"]}),
    ):
        groups = db.whatsapp_connections.aggregate([
            {"$match": {"is_active": {"$ne": False}, "org_id": {"$type": "string"}}},
            {"$project": {"owner": "$org_id", "identifier": canonical}},
            {"$match": {"identifier": {"$type": "string"}}},
            {"$group": {"_id": "$identifier", "owners": {"$addToSet": "$owner"}}},
            {"$match": {"owners.1": {"$exists": True}}},
            {"$limit": max_items + 1},
        ], allowDiskUse=True)
        errors.extend(
            f"{label} {group['_id']} belongs to multiple organizations: {sorted(group['owners'])}"
            for group in groups
        )
    return errors


def _apply_org(
    db,
    org_id: str,
    actions: dict,
    now: datetime,
    renew_lease=lambda: None,
    max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
) -> None:
    if renew_lease() is False:
        raise RuntimeError(f"Organization {org_id} lost its migration lease")
    primary = next((item for item in actions["updates"] if item["slot"] == 1), None)
    mapping = conversation_mapping_plan(
        _conversation_documents(db, org_id, max_conversations),
        actions["updates"],
        primary["phone_id"],
    ) if primary else {"reassignment_count": 0, "unresolved_count": 0, "assignments": {}}
    if mapping["unresolved_count"]:
        raise RuntimeError(
            f"Organization {org_id} has {mapping['unresolved_count']} conversations "
            "that cannot be mapped to a WhatsApp number"
        )
    for wa_id in sorted({str(item["wa_id"]) for item in actions["updates"]}):
        try:
            db.whatsapp_waba_owners.update_one(
                {"_id": wa_id, "$or": [{"org_id": org_id}, {"org_id": {"$exists": False}}]},
                {"$set": {"org_id": org_id, "updated_at": now}, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        except DuplicateKeyError as exc:
            raise RuntimeError(f"WABA {wa_id} is already owned by another organization") from exc
    for duplicate_id in actions["delete_ids"]:
        duplicate = db.whatsapp_connections.find_one({"_id": duplicate_id}) or {}
        db.whatsapp_connections.update_one(
            {"_id": duplicate_id},
            {"$set": {
                "phone_number_id": str(duplicate.get("phone_number_id") or duplicate.get("phone_id") or ""),
                "waba_id": str(duplicate.get("waba_id") or duplicate.get("wa_id") or ""),
                "is_active": False,
                "migration_status": "superseded",
                "updated_at": now,
            }, "$unset": {
                "access_token": "", "phone_id": "", "wa_id": "", "slot": "",
                "label_key": "", "wa_bot_flow_id": "",
            }},
        )

    for update in actions["updates"]:
        fields = {key: value for key, value in update.items() if key != "_id"}
        db.whatsapp_connections.update_one(
            {"_id": update["_id"]},
            {"$set": {**fields, "is_active": True, "updated_at": now}},
        )
    conversations = db[f"conversations_{org_id}"]
    if not primary:
        conversations.create_index(
            [("platform", 1), ("business_phone_id", 1), ("customer_id", 1)],
            unique=True,
            partialFilterExpression={
                "platform": "whatsapp",
                "business_phone_id": {"$type": "string"},
                "customer_id": {"$type": "string"},
            },
            name="whatsapp_phone_customer_unique",
        )
        return

    db.organizations.update_one({"org_id": org_id}, {"$set": {"wa_id": primary["wa_id"]}})
    _apply_conversation_mapping(db, org_id, mapping, actions["updates"])
    actions["conversation_reassignment_count"] = mapping["reassignment_count"]
    if renew_lease() is False:
        raise RuntimeError(f"Organization {org_id} lost its migration lease")
    actions["merged_conversations"] = _merge_duplicate_conversations(db, org_id, primary["phone_id"], now)
    if renew_lease() is False:
        raise RuntimeError(f"Organization {org_id} lost its migration lease")
    if actions.get("active_flow_id"):
        db.whatsapp_connections.update_one(
            {"org_id": org_id, "slot": 1}, {"$set": {"wa_bot_flow_id": actions["active_flow_id"]}},
        )
    conversations.create_index(
        [("platform", 1), ("business_phone_id", 1), ("customer_id", 1)],
        unique=True,
        partialFilterExpression={
            "platform": "whatsapp",
            "business_phone_id": {"$type": "string"},
            "customer_id": {"$type": "string"},
        },
        name="whatsapp_phone_customer_unique",
    )


def _create_and_verify_indexes(db, org_ids: list[str]) -> None:
    db.whatsapp_connections.create_index(
        "phone_id", unique=True,
        partialFilterExpression={"phone_id": {"$type": "string"}, "is_active": True},
        name="whatsapp_phone_unique",
    )
    db.whatsapp_connections.create_index(
        [("org_id", 1), ("slot", 1)], unique=True,
        partialFilterExpression={"org_id": {"$type": "string"}, "slot": {"$type": "number"}, "is_active": True},
        name="whatsapp_org_slot_unique",
    )
    db.whatsapp_connections.create_index(
        [("org_id", 1), ("label_key", 1)], unique=True,
        partialFilterExpression={"org_id": {"$type": "string"}, "label_key": {"$type": "string"}, "is_active": True},
        name="whatsapp_org_label_unique",
    )
    required = {"whatsapp_phone_unique", "whatsapp_org_slot_unique", "whatsapp_org_label_unique"}
    if not required.issubset(db.whatsapp_connections.index_information()):
        raise RuntimeError("Multi-number WhatsApp connection indexes could not be verified")
    for org_id in org_ids:
        if "whatsapp_phone_customer_unique" not in db[f"conversations_{org_id}"].index_information():
            raise RuntimeError(f"WhatsApp conversation index could not be verified for {org_id}")


def repair_mapping(
    *,
    apply: bool = False,
    org_id: str,
    primary_phone_id: str,
    max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
) -> dict:
    """Repair one migrated organization from preserved conversation WABA IDs."""
    from database import get_mongo_db

    db = get_mongo_db()
    org_id = str(org_id or "").strip()
    primary_phone_id = str(primary_phone_id or "").strip()
    report = {
        "errors": [],
        "dry_run": not apply,
        "applied": False,
        "org_id": org_id,
        "primary_phone_id": primary_phone_id,
    }
    connections = list(db.whatsapp_connections.find({
        "org_id": org_id,
        "is_active": {"$ne": False},
    }).sort([("slot", 1), ("connected_at", 1)]))
    matches = [
        item for item in connections
        if str(item.get("phone_id") or item.get("phone_number_id") or "") == primary_phone_id
    ]
    if len(matches) != 1:
        report["errors"].append(
            f"Organization {org_id} does not have exactly one active WhatsApp connection {primary_phone_id}"
        )
        return report
    target = matches[0]
    current = next((item for item in connections if item.get("slot") == 1), None)
    current_flow_id = str((current or {}).get("wa_bot_flow_id") or "")
    target_flow_id = str(target.get("wa_bot_flow_id") or "")
    if current and current.get("_id") != target.get("_id") and current_flow_id and target_flow_id != current_flow_id:
        if target_flow_id:
            report["errors"].append(
                "The requested primary and current primary have different WaBot flows; repair them explicitly first"
            )

    try:
        mapping = conversation_mapping_plan(
            _conversation_documents(db, org_id, max_conversations),
            connections,
            primary_phone_id,
            remap_unidentified_to_primary=True,
        )
    except ValueError as exc:
        report["errors"].append(str(exc))
        return report
    report.update({
        "conversation_reassignment_count": mapping["reassignment_count"],
        "conversation_reassignments_by_phone_id": mapping["reassignments_by_phone_id"],
        "conversation_collision_count": mapping["collision_count"],
        "unresolved_conversation_count": mapping["unresolved_count"],
        "primary_phone": target.get("phone"),
    })
    if mapping["collision_count"]:
        report["errors"].append(
            f"Repair would create {mapping['collision_count']} duplicate sender/customer conversations"
        )
    if mapping["unresolved_count"]:
        report["errors"].append(
            f"Repair cannot identify the WhatsApp number for {mapping['unresolved_count']} conversations"
        )
    if report["errors"] or not apply:
        return report

    _apply_conversation_mapping(db, org_id, mapping, connections)
    now = datetime.now(timezone.utc)
    reordered = [target, *(item for item in connections if item.get("_id") != target.get("_id"))]
    with db.client.start_session() as session:
        with session.start_transaction():
            for index, connection in enumerate(reordered, start=1):
                db.whatsapp_connections.update_one(
                    {"_id": connection["_id"]},
                    {"$set": {"slot": -index, "updated_at": now}},
                    session=session,
                )
            for slot, connection in enumerate(reordered, start=1):
                db.whatsapp_connections.update_one(
                    {"_id": connection["_id"]},
                    {"$set": {"slot": slot, "updated_at": now}},
                    session=session,
                )
            db.organizations.update_one(
                {"org_id": org_id},
                {"$set": {"wa_id": str(target.get("wa_id") or target.get("waba_id") or "")}},
                session=session,
            )
            if current and current.get("_id") != target.get("_id") and current_flow_id:
                db.whatsapp_connections.update_one(
                    {"_id": current["_id"], "wa_bot_flow_id": current_flow_id},
                    {"$unset": {"wa_bot_flow_id": ""}},
                    session=session,
                )
                db.whatsapp_connections.update_one(
                    {"_id": target["_id"]},
                    {"$set": {"wa_bot_flow_id": current_flow_id}},
                    session=session,
                )
            db.system_migrations.update_one(
                {"_id": f"{MIGRATION_ID}:line_mapping:{org_id}"},
                {"$set": {
                    "migration_id": MIGRATION_ID,
                    "org_id": org_id,
                    "status": "complete",
                    "primary_phone_id": primary_phone_id,
                    "conversation_reassignment_count": mapping["reassignment_count"],
                    "completed_at": now,
                }},
                upsert=True,
                session=session,
            )
    report["applied"] = True
    return report


def migrate(
    apply: bool = False,
    org_id: str | None = None,
    org_batch_size: int = DEFAULT_ORG_BATCH_SIZE,
    max_rows_per_org: int = DEFAULT_MAX_ROWS_PER_ORG,
    report_items: int = DEFAULT_REPORT_ITEMS,
    max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
) -> dict:
    from database import get_mongo_db

    db = get_mongo_db()
    all_org_ids = sorted(str(value) for value in db.whatsapp_connections.distinct("org_id") if value)
    if org_id:
        requested = str(org_id).strip()
        all_selected_ids = [requested] if requested in all_org_ids else []
    else:
        all_selected_ids = all_org_ids
    completed = {
        str(row.get("org_id") or "")
        for row in db.system_migrations.find({
            "migration_id": MIGRATION_ID,
            "status": {"$in": ["data_complete", "complete"]},
        }, {"org_id": 1})
    } if apply else set()
    pending_ids = [value for value in all_selected_ids if value not in completed]
    selected_ids = pending_ids[:max(1, int(org_batch_size))]
    report = {
        "errors": _cross_org_ownership_errors(db, max(1, report_items)),
        "organizations": {},
        "dry_run": not apply,
        "applied": False,
        "selected_org_count": len(selected_ids),
        "remaining_org_count": max(0, len(pending_ids) - len(selected_ids)),
    }
    if org_id and not all_selected_ids:
        report["errors"].append(f"Organization {org_id} has no WhatsApp connections")

    for selected_org_id in selected_ids:
        rows = list(db.whatsapp_connections.find({"org_id": selected_org_id}).limit(max_rows_per_org + 1))
        if len(rows) > max_rows_per_org:
            report["errors"].append(
                f"Organization {selected_org_id} exceeds the bounded {max_rows_per_org}-row migration limit"
            )
            continue
        organization = db.organizations.find_one({"org_id": selected_org_id}, {"wa_id": 1}) or {}
        org_plan = build_plan(rows, {selected_org_id: str(organization.get("wa_id") or "")})
        report["errors"].extend(org_plan["errors"])
        actions = org_plan["organizations"].get(selected_org_id, {"updates": [], "delete_ids": []})
        active_flows = list(db[f"wa_bot_flows_{selected_org_id}"].find(
            {"status": "active"}, {"flow_id": 1},
        ).limit(2))
        if len(active_flows) > 1:
            report["errors"].append(f"Organization {selected_org_id} has multiple active WaBot flows")
        actions["active_flow_id"] = active_flows[0].get("flow_id") if len(active_flows) == 1 else None
        primary = next((item for item in actions["updates"] if item["slot"] == 1), None)
        if primary:
            try:
                mapping = conversation_mapping_plan(
                    _conversation_documents(db, selected_org_id, max_conversations),
                    actions["updates"],
                    primary["phone_id"],
                )
                actions["conversation_reassignment_count"] = mapping["reassignment_count"]
                actions["conversation_reassignments_by_phone_id"] = mapping["reassignments_by_phone_id"]
                actions["conversation_merge_count"] = mapping["collision_count"]
                actions["unresolved_conversation_count"] = mapping["unresolved_count"]
                if mapping["unresolved_count"]:
                    report["errors"].append(
                        f"Organization {selected_org_id} has {mapping['unresolved_count']} conversations "
                        "that cannot be mapped to a WhatsApp number"
                    )
            except ValueError as exc:
                report["errors"].append(str(exc))
        report["organizations"][selected_org_id] = actions

    report["errors"] = sorted(set(report["errors"]))
    if not apply or report["errors"]:
        return report

    owner = uuid.uuid4().hex
    for selected_org_id, actions in report["organizations"].items():
        now = datetime.now(timezone.utc)
        if not claim_org_lease(db, selected_org_id, owner, now):
            report["errors"].append(f"Organization {selected_org_id} migration is already running or complete")
            continue
        try:
            _apply_org(
                db,
                selected_org_id,
                actions,
                now,
                renew_lease=lambda: renew_org_lease(
                    db, selected_org_id, owner, datetime.now(timezone.utc),
                ),
                max_conversations=max_conversations,
            )
            if not checkpoint_org(db, selected_org_id, owner, "data_complete", now):
                raise RuntimeError(f"Organization {selected_org_id} lost its migration lease")
        except Exception as exc:
            checkpoint_org(db, selected_org_id, owner, "failed", datetime.now(timezone.utc), error=type(exc).__name__)
            raise

    all_data_complete = all(
        db.system_migrations.find_one({
            "_id": _org_checkpoint_id(value), "status": {"$in": ["data_complete", "complete"]},
        })
        for value in all_org_ids
    )
    target_data_complete = bool(org_id and all_selected_ids and db.system_migrations.find_one({
        "_id": _org_checkpoint_id(all_selected_ids[0]),
        "status": {"$in": ["data_complete", "complete"]},
    }))
    ready_org_ids = all_org_ids if all_data_complete else []
    if target_data_complete and not ready_org_ids:
        ready_org_ids = all_selected_ids
    if ready_org_ids:
        _create_and_verify_indexes(db, ready_org_ids)
        now = datetime.now(timezone.utc)
        for value in ready_org_ids:
            db.organizations.update_one(
                {"org_id": value}, {"$set": {"whatsapp_connection_schema_version": 2}},
            )
            db.system_migrations.update_one(
                {"_id": _org_checkpoint_id(value)},
                {"$set": {"status": "complete", "completed_at": now}},
            )
        if all_data_complete:
            db.system_migrations.update_one(
                {"_id": MIGRATION_ID}, {"$set": {"status": "complete", "completed_at": now}}, upsert=True,
            )
        report["verified_indexes"] = True
    else:
        report["has_more"] = True
    report["applied"] = bool(report["organizations"] or ready_org_ids)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply a clean migration plan")
    parser.add_argument("--org-id", help="Migrate only one organization")
    parser.add_argument("--org-batch-size", type=int, default=DEFAULT_ORG_BATCH_SIZE)
    parser.add_argument("--max-rows-per-org", type=int, default=DEFAULT_MAX_ROWS_PER_ORG)
    parser.add_argument("--max-conversations", type=int, default=DEFAULT_MAX_CONVERSATIONS)
    parser.add_argument("--max-report-items", type=int, default=DEFAULT_REPORT_ITEMS)
    parser.add_argument(
        "--repair-primary-phone-id",
        help="Repair one migrated organization and make this phone ID primary",
    )
    args = parser.parse_args()
    if args.repair_primary_phone_id and not args.org_id:
        parser.error("--repair-primary-phone-id requires --org-id")
    if args.repair_primary_phone_id:
        report = repair_mapping(
            apply=args.apply,
            org_id=args.org_id,
            primary_phone_id=args.repair_primary_phone_id,
            max_conversations=args.max_conversations,
        )
    else:
        report = migrate(
            apply=args.apply,
            org_id=args.org_id,
            org_batch_size=args.org_batch_size,
            max_rows_per_org=args.max_rows_per_org,
            report_items=args.max_report_items,
            max_conversations=args.max_conversations,
        )
    print(json.dumps(bounded_report(report, args.max_report_items), default=str, indent=2))
    if report["errors"]:
        raise SystemExit(1)
