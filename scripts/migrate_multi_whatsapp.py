"""Validate and backfill the multi-number WhatsApp model.

Dry-run is the default. Apply only after the report has no errors:

    python -m scripts.migrate_multi_whatsapp
    python -m scripts.migrate_multi_whatsapp --apply
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone

from services.whatsapp_connection_service import MAX_WHATSAPP_CONNECTIONS

MIGRATION_ID = "multi_whatsapp_v2"


def _connected_at(connection: dict) -> str:
    value = connection.get("connected_at")
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


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
            for collection_name in (f"messages_{org_id}", f"private_notes_{org_id}"):
                db[collection_name].update_many(
                    {"conversation_id": {"$in": old_ids}},
                    {"$set": {"conversation_id": canonical_id}},
                )
            for collection_name in ("scheduled_messages", "automation_scheduled_followups"):
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


def build_plan(connections: list[dict]) -> dict:
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
            ordered = sorted(active, key=_connected_at, reverse=True)
            distinct.append(ordered[0])
            duplicate_ids.extend(item["_id"] for item in ordered[1:])
            duplicate_ids.extend(item["_id"] for item in same_phone if item.get("is_active") is False)

        if len(distinct) > MAX_WHATSAPP_CONNECTIONS:
            errors.append(f"Organization {org_id} has {len(distinct)} distinct WhatsApp numbers")
        used_labels: set[str] = set()
        updates = []
        for slot, connection in enumerate(sorted(distinct, key=_connected_at), start=1):
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


def migrate(apply: bool = False) -> dict:
    from database import get_mongo_db

    db = get_mongo_db()
    if apply and db.system_migrations.find_one({"_id": MIGRATION_ID, "status": "complete"}):
        return {"errors": [], "organizations": {}, "verified_indexes": True, "applied": False}

    raw_connections = list(db.whatsapp_connections.find({}))
    plan = build_plan(raw_connections)

    for org_id, actions in plan["organizations"].items():
        active_flows = list(db[f"wa_bot_flows_{org_id}"].find({"status": "active"}, {"flow_id": 1}))
        if len(active_flows) > 1:
            plan["errors"].append(f"Organization {org_id} has multiple active WaBot flows")
        actions["active_flow_id"] = active_flows[0].get("flow_id") if len(active_flows) == 1 else None
        primary = next((item for item in actions["updates"] if item["slot"] == 1), None)
        if not apply:
            actions["conversation_merge_count"] = sum(
                max(0, group["count"] - 1)
                for group in _duplicate_conversation_groups(
                    db[f"conversations_{org_id}"], (primary or {}).get("phone_id", "")
                )
            )

    plan["errors"].extend(organization_waba_errors(list(db.organizations.find(
        {"wa_id": {"$type": "string"}}, {"_id": 1, "org_id": 1, "wa_id": 1},
    ))))

    plan["errors"] = sorted(set(plan["errors"]))
    if not apply or plan["errors"]:
        return plan

    now = datetime.now(timezone.utc)
    for org_id, actions in plan["organizations"].items():
        if actions["delete_ids"]:
            for duplicate_id in actions["delete_ids"]:
                duplicate = db.whatsapp_connections.find_one({"_id": duplicate_id}) or {}
                db.whatsapp_connections.update_one(
                    {"_id": duplicate_id},
                    {
                        "$set": {
                            "phone_number_id": str(duplicate.get("phone_number_id") or duplicate.get("phone_id") or ""),
                            "waba_id": str(duplicate.get("waba_id") or duplicate.get("wa_id") or ""),
                            "is_active": False,
                            "migration_status": "superseded",
                            "updated_at": now,
                        },
                        "$unset": {
                            "access_token": "", "phone_id": "", "wa_id": "", "slot": "",
                            "label_key": "", "wa_bot_flow_id": "",
                        },
                    },
                )

        primary = None
        for update in actions["updates"]:
            if update["slot"] == 1:
                primary = update
            fields = {key: value for key, value in update.items() if key != "_id"}
            fields["is_active"] = True
            fields["updated_at"] = now
            db.whatsapp_connections.update_one({"_id": update["_id"]}, {"$set": fields})

        if not primary:
            continue
        db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"wa_id": primary["wa_id"]}},
        )
        conversations = db[f"conversations_{org_id}"]
        conversations.update_many(
            {"platform": "whatsapp", "$or": [
                {"business_phone_id": {"$exists": False}}, {"business_phone_id": None},
            ]},
            {"$set": {"business_phone_id": primary["phone_id"], "business_phone": primary.get("phone")}},
        )
        actions["merged_conversations"] = _merge_duplicate_conversations(
            db, org_id, primary["phone_id"], now,
        )

        contacts = db[f"contacts_{org_id}"]
        for contact in contacts.find({"platform": "whatsapp", "phone_number": {"$type": "string"}}, {"phone_number": 1}):
            conversation_ids = [item["conversation_id"] for item in conversations.find(
                {"platform": "whatsapp", "customer_id": contact["phone_number"]}, {"conversation_id": 1}
            ) if item.get("conversation_id")]
            if conversation_ids:
                contacts.update_one(
                    {"_id": contact["_id"]},
                    {"$addToSet": {"conversation_ids": {"$each": conversation_ids}}},
                )

        if actions.get("active_flow_id"):
            db.whatsapp_connections.update_one(
                {"org_id": org_id, "slot": 1},
                {"$set": {"wa_bot_flow_id": actions["active_flow_id"]}},
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
    db.organizations.create_index(
        "wa_id", unique=True, partialFilterExpression={"wa_id": {"$type": "string"}},
        name="organization_waba_unique",
    )
    required_connection_indexes = {
        "whatsapp_phone_unique", "whatsapp_org_slot_unique", "whatsapp_org_label_unique",
    }
    if not required_connection_indexes.issubset(db.whatsapp_connections.index_information()):
        raise RuntimeError("Multi-number WhatsApp connection indexes could not be verified")
    if "organization_waba_unique" not in db.organizations.index_information():
        raise RuntimeError("WhatsApp organization ownership index could not be verified")
    for org_id, actions in plan["organizations"].items():
        if not actions["updates"]:
            continue
        if "whatsapp_phone_customer_unique" not in db[f"conversations_{org_id}"].index_information():
            raise RuntimeError(f"WhatsApp conversation index could not be verified for {org_id}")
        db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"whatsapp_connection_schema_version": 2}},
        )
    plan["verified_indexes"] = True
    plan["applied"] = True
    db.system_migrations.update_one(
        {"_id": MIGRATION_ID},
        {"$set": {"status": "complete", "completed_at": now}},
        upsert=True,
    )
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply a clean migration plan")
    args = parser.parse_args()
    report = migrate(apply=args.apply)
    print(json.dumps(report, default=str, indent=2))
    if report["errors"]:
        raise SystemExit(1)
