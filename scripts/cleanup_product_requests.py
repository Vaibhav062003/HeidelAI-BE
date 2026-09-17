"""Review saved product requests, then apply that exact report without another model call.

python -m scripts.cleanup_product_requests --org-id ORG --report /tmp/requests.json
python -m scripts.cleanup_product_requests --org-id ORG --apply-report /tmp/requests.json
"""
import argparse
import asyncio
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from services.product_request_policy import as_utc, grounded_request, product_name, request_key

MESSAGE_FIELDS = {field: 1 for field in ("_id", "message_id", "conversation_id", "role", "mode", "content", "timestamp")}
CONVERSATION_FIELDS = {field: 1 for field in ("_id", "conversation_id", "platform", "customer_id", "business_phone_id", "is_dealer", "persona")}


def fingerprint(value):
    def encode(item):
        return as_utc(item).isoformat() if isinstance(item, datetime) else str(item)
    return hashlib.sha256(json.dumps(value, default=encode, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def protected(row):
    return bool(row.get("reviewed_by") or row.get("product_id") or row.get("consent_note")
                or any(value for key, value in row.items() if key.startswith("notification_") and key != "notification_requested"))


def plan_conversation(rows, extracted, report_id, stamp):
    """Pure plan: preserve IDs, retain originals, split only unreviewed requests."""
    candidates = defaultdict(list)
    targets = {}
    for row in rows:
        values = extracted[str(row["_id"])]
        same_product = [item for item in values if request_key(row["org_id"], row["conversation_id"], item["product_name"]) == request_key(row["org_id"], row["conversation_id"], row["product_name"])]
        if same_product:
            values = same_product
        elif product_name(row.get("product_name")) is not None:
            return {"review": "Existing product has no exact extracted identity match; preserve it for review"}
        keys = {request_key(row["org_id"], row["conversation_id"], item["product_name"]) for item in values}
        targets[str(row["_id"])] = keys
        if protected(row) and (len(keys) != 1 or next(iter(keys)) != request_key(row["org_id"], row["conversation_id"], row["product_name"])):
            return {"review": "Product correction or split affects a reviewed/permission/outreach record"}
        for item in values:
            key = request_key(row["org_id"], row["conversation_id"], item["product_name"])
            candidates[key].append((row, item))
    after = {str(row["_id"]): deepcopy(row) for row in rows}
    retained = set()
    changes = []
    winners = {}
    for key, entries in candidates.items():
        reviewed = {str(row["_id"]) for row, _ in entries if protected(row)}
        if len(reviewed) > 1:
            return {"review": "Duplicate products have multiple reviewed/permission/outreach records"}
        owners = {str(row["_id"]) for row in rows if row.get("product_key") == key}
        if owners and (len(owners) != 1 or reviewed and reviewed != owners
                       or any(targets[owner] != {key} for owner in owners)):
            return {"review": "Canonical product identity conflicts with its existing owner"}
        statuses = {row.get("status", "requested") for row, _ in entries}
        if len(statuses) > 1:
            return {"review": "Duplicate products have conflicting interest statuses"}
        # A one-product source can retain its ID; a build's original row is archived.
        eligible = [(row, item) for row, item in entries if len(targets[str(row["_id"])]) == 1]
        eligible.sort(key=lambda pair: (str(pair[0]["_id"]) not in owners, not protected(pair[0]), str(pair[0]["_id"])))
        latest_row, latest = max(entries, key=lambda pair: as_utc(pair[1]["source_at"]))
        if eligible:
            original = eligible[0][0]
            target = after[str(original["_id"])]
            retained.add(str(original["_id"]))
        else:
            original = None
            target = {field: latest_row[field] for field in (
                "org_id", "conversation_id", "customer_name", "customer_phone", "business_phone_id", "business_phone"
            ) if field in latest_row}
            target.update(_id=str(uuid4()), status=latest_row.get("status", "requested"), created_at=stamp)
            after[str(target["_id"])] = target
        winners[key] = str(target["_id"])
        update = {field: latest[field] for field in (
            "product_name", "evidence", "source_message_id", "product_reference_message_id", "product_mention", "specificity", "confidence", "notification_requested"
        )}
        update.update(product_key=key, last_asked_at=latest["source_at"])
        if latest.get("withdrawn"):
            update["status"] = "dismissed"
        if any(target.get(field) != value for field, value in update.items()):
            if original and "cleanup_original" not in target:
                target["cleanup_original"] = deepcopy(original)
            target.update(update, cleanup_report_id=report_id)
            changes.append({"action": "correct" if original else "component", "id": str(target["_id"]), "product": target["product_name"]})
    for row in rows:
        if str(row["_id"]) in retained:
            continue
        target = after[str(row["_id"])]
        target.update(archived_at=stamp, archive_reason="replaced_by_products" if targets[str(row["_id"])] else "not_a_specific_product",
                      cleanup_report_id=report_id)
        target["replaced_by"] = sorted(winners[key] for key in targets[str(row["_id"])])
        if "cleanup_original" not in target:
            target["cleanup_original"] = deepcopy(row)
        target.pop("product_key", None)
        changes.append({"action": "archive", "id": str(row["_id"]), "product": row["product_name"]})
    return {"before": rows, "after": list(after.values()), "changes": changes}


async def make_report(db, org, extract):
    report_id, stamp = str(uuid4()), datetime.now(timezone.utc)
    report = {"version": 2, "report_id": report_id, "org_id": org, "created_at": stamp, "conversations": [], "review": []}
    grouped = defaultdict(list)
    async for row in db.product_requests.find({"org_id": org, "archived_at": {"$exists": False}}).sort("_id", 1):
        grouped[row["conversation_id"]].append(row)
    for cid, rows in grouped.items():
        extracted, contexts, error = {}, {}, None
        conversation = await db[f"conversations_{org}"].find_one({"conversation_id": cid}, CONVERSATION_FIELDS)
        if not conversation or conversation.get("platform") != "whatsapp" or conversation.get("is_dealer") or conversation.get("persona") in {"dealer", "dealers"}:
            report["review"].append({"conversation_id": cid, "reason": "Missing or ineligible customer conversation"})
            continue
        for row in rows:
            source_id = str(row.get("source_message_id") or "")
            if source_id in contexts:
                source = contexts[source_id]["messages"][-1]
            else:
                ids = [source_id]
                if len(source_id) == 24:
                    from bson import ObjectId
                    if ObjectId.is_valid(source_id):
                        ids.append(ObjectId(source_id))
                source = await db[f"messages_{org}"].find_one({"conversation_id": cid, "$or": [{"message_id": source_id}, {"_id": {"$in": ids}}]}, MESSAGE_FIELDS)
            if not source or not isinstance(source.get("timestamp"), datetime):
                error = "Missing source message/timestamp; cannot safely re-extract"
                break
            evidence = str(row.get("evidence") or "").strip()
            if (source.get("role") != "customer" or source.get("mode") == "private"
                    or not evidence or evidence not in str(source.get("content") or "")):
                error = "Original customer evidence is missing or invalid; preserve the request for review"
                break
            if source_id not in contexts:
                docs = await db[f"messages_{org}"].find({"conversation_id": cid, "timestamp": {"$lte": source["timestamp"]}}, MESSAGE_FIELDS).sort([("timestamp", -1), ("_id", -1)]).limit(30).to_list(30)
                docs.reverse()
                # Equal timestamps must not permit messages after the actual source as references.
                source_index = next((i for i, message in enumerate(docs) if message["_id"] == source["_id"]), None)
                if source_index is None:
                    error = "Source is outside its reconstruction window"
                    break
                docs = docs[:source_index + 1]
                def lines(values):
                    return "\n".join(f"[{m.get('message_id') or m.get('_id')}] {m.get('role', 'unknown')}: {m.get('content', '')}" for m in values if m.get("mode") != "private")
                text = "--- OLDER CONTEXT (For product reference only) ---\n" + lines(docs[:-10]) + "\n--- RECENT MESSAGES ---\n" + lines(docs[-10:])
                result = await extract(text, as_utc(source["timestamp"]).isoformat())
                if result is None:
                    error = "Extraction failed; no records may be archived"
                    break
                raw = [item.model_dump() for item in result.product_requests]
                if any(item.get("source_message_id") != source_id for item in raw):
                    error = "Extraction cites another source message; preserve requests for review"
                    break
                if any(item.get("confidence") == "low" or item.get("specificity") == "ambiguous" for item in raw):
                    error = "Ambiguous extraction requires review"
                    break
                valid = [item for value in raw if (item := grounded_request(value, docs))]
                if any(value.get("specificity") == "specific_product" and not grounded_request(value, docs) for value in raw):
                    error = "Specific product failed grounding; review instead of archiving"
                    break
                contexts[source_id] = {"messages": docs, "items": valid}
            extracted[str(row["_id"])] = contexts[source_id]["items"]
        if error:
            report["review"].append({"conversation_id": cid, "reason": error})
            continue
        plan = plan_conversation(rows, extracted, report_id, stamp)
        if "review" in plan:
            report["review"].append({"conversation_id": cid, "reason": plan["review"]})
        elif plan["changes"]:
            report["conversations"].append({"conversation_id": cid, **plan, "contexts": list(contexts.values()), "conversation": conversation})
    return report


def same_rows(actual, expected):
    return fingerprint(sorted(actual, key=lambda row: str(row["_id"]))) == fingerprint(sorted(expected, key=lambda row: str(row["_id"])))


async def apply_report(db, org, report):
    if report.get("version") != 2:
        raise ValueError("Unsupported cleanup report version; regenerate and review a version 2 report")
    if report.get("org_id") != org or not report.get("report_id"):
        raise ValueError("Report organization or identifier does not match")
    await db.product_requests.create_index("product_key", unique=True, sparse=True)
    applied = already_applied = 0
    for plan in report["conversations"]:
        cid = plan["conversation_id"]
        before, after = plan["before"], plan["after"]
        if any(row.get("org_id") != org or row.get("conversation_id") != cid for row in before + after):
            raise ValueError("Report contains a row outside its organization/conversation")
        if any(row.get("product_key") and row["product_key"] != request_key(org, cid, row["product_name"]) for row in after):
            raise ValueError("Report has an invalid product identity")
        # One conversation is atomic. No model call, follow-up scheduler or sender is invoked.
        async with await db.client.start_session() as session:
            async with session.start_transaction():
                query = {"org_id": org, "conversation_id": cid, "$or": [{"archived_at": {"$exists": False}}, {"cleanup_report_id": report["report_id"]}]}
                current = await db.product_requests.find(query, session=session).sort("_id", 1).to_list(None)
                if same_rows(current, after):
                    already_applied += 1
                    continue
                if not same_rows(current, before):
                    raise ValueError(f"Stale report: requests changed for {cid}; regenerate and review")
                conversation = await db[f"conversations_{org}"].find_one({"conversation_id": cid}, CONVERSATION_FIELDS, session=session)
                if fingerprint(conversation) != fingerprint(plan["conversation"]):
                    raise ValueError(f"Stale report: conversation changed for {cid}")
                ids = [row["_id"] for row in before]
                if await db.restock_outbox.find_one({"org_id": org, "request_id": {"$in": ids}, "status": {"$in": ["preparing", "queued", "checking", "sending"]}}, session=session):
                    raise ValueError(f"Pending outreach for {cid}; resolve it before cleanup")
                for context in plan["contexts"]:
                    docs = context["messages"]
                    current_messages = await db[f"messages_{org}"].find({"conversation_id": cid, "_id": {"$in": [m["_id"] for m in docs]}}, MESSAGE_FIELDS, session=session).to_list(None)
                    if not same_rows(current_messages, docs):
                        raise ValueError(f"Stale report: evidence changed for {cid}")
                # Existing canonical keys remain on their owners; no unique-key transfer is needed.
                for row in after:
                    if row.get("archived_at"):
                        await db.product_requests.replace_one({"org_id": org, "_id": row["_id"]}, row, session=session)
                old_ids = {row["_id"] for row in before}
                for row in after:
                    if row.get("archived_at"):
                        continue
                    if row["_id"] in old_ids:
                        await db.product_requests.replace_one({"org_id": org, "_id": row["_id"]}, row, session=session)
                    else:
                        await db.product_requests.insert_one(row, session=session)
                applied += 1
    return {"applied_conversations": applied, "already_applied": already_applied, "needs_review": len(report.get("review", []))}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", type=Path, help="Write a dry-run report; does not modify the database")
    mode.add_argument("--apply-report", type=Path, help="Apply the exact reviewed report")
    args = parser.parse_args()
    from bson import json_util
    from database import get_async_mongo_db
    db = get_async_mongo_db()
    if args.apply_report:
        report = json_util.loads(args.apply_report.read_text())
        print(json.dumps(await apply_report(db, args.org_id, report)))
    else:
        from agents.intent_agent.agent import extract_purchase_intent
        report = await make_report(db, args.org_id, extract_purchase_intent)
        with os.fdopen(os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            output.write(json_util.dumps(report, indent=2))
        print(json.dumps({"report": str(args.report), "changed_conversations": len(report["conversations"]), "needs_review": len(report["review"])}))


if __name__ == "__main__":
    asyncio.run(main())
