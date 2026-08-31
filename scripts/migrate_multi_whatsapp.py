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


def conversation_key_errors(org_id: str, conversations: list[dict], primary_phone_id: str) -> list[str]:
    seen = {}
    errors = []
    for conversation in conversations:
        customer_id = str(conversation.get("customer_id") or "").strip()
        phone_id = str(conversation.get("business_phone_id") or primary_phone_id or "").strip()
        if not customer_id or not phone_id:
            errors.append(f"WhatsApp conversation {conversation.get('_id')} in {org_id} has no customer or sender id")
            continue
        key = (phone_id, customer_id)
        if key in seen:
            errors.append(f"WhatsApp conversations {seen[key]} and {conversation.get('_id')} in {org_id} share sender/customer {key}")
        else:
            seen[key] = conversation.get("_id")
    return errors


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

        waba_ids = {str(item["wa_id"]) for item in distinct}
        if len(distinct) > MAX_WHATSAPP_CONNECTIONS:
            errors.append(f"Organization {org_id} has {len(distinct)} distinct WhatsApp numbers")
        if len(waba_ids) > 1:
            errors.append(f"Organization {org_id} has mixed WABAs: {sorted(waba_ids)}")
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
        conversations = list(db[f"conversations_{org_id}"].find({"platform": "whatsapp"}, {
            "_id": 1, "customer_id": 1, "business_phone_id": 1,
        }))
        plan["errors"].extend(conversation_key_errors(org_id, conversations, (primary or {}).get("phone_id", "")))

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
            partialFilterExpression={"platform": "whatsapp", "business_phone_id": {"$type": "string"}},
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
