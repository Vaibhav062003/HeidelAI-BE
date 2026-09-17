"""Daily floor maintenance using the existing Sheet writer and stock audit."""
import hashlib
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import get_async_mongo_db
from services.pricing_policy import normalize_inr
from services.product_request_service import catalogue_org
from services.work_team_service import conversation_access_condition

ADMIN = {"admin", "org:admin"}
HELD = {"publishing", "uncertain"}


def now():
    return datetime.now(timezone.utc)


async def product_by_id(org, product_id):
    ids = [product_id] + ([ObjectId(product_id)] if ObjectId.is_valid(product_id) else [])
    return await get_async_mongo_db()[f"products_{org}"].find_one({"_id": {"$in": ids}})


async def daily_prices(user, q="", page=1, requested_only=False):
    import re
    db = get_async_mongo_db()
    catalog = await catalogue_org(user.org_id)
    access = await conversation_access_condition(user.org_id, user.user_id, user.role)
    match = {"status": {"$ne": "withdrawn"}}
    if q.strip():
        match["$or"] = [{key: {"$regex": re.escape(q.strip()), "$options": "i"}}
                        for key in ("model_no", "sku", "product_name", "model_name", "unique_identifier")]
    pipeline = [{"$match": match}, {"$lookup": {
        "from": "product_requests", "let": {"pid": {"$toString": "$_id"}}, "pipeline": [
            {"$match": {"org_id": user.org_id, "catalogue_org_id": catalog,
                "archived_at": {"$exists": False},
                "last_asked_at": {"$gte": now() - timedelta(days=7)},
                "$expr": {"$eq": ["$product_id", "$$pid"]}}},
            {"$lookup": {"from": f"conversations_{user.org_id}", "let": {"cid": "$conversation_id"},
                "pipeline": [{"$match": {"$and": [access, {"$expr": {"$eq": ["$conversation_id", "$$cid"]}}]}}], "as": "access"}},
            {"$match": {"access.0": {"$exists": True}}},
            {"$group": {"_id": {"$cond": [{"$gt": [{"$strLenCP": {"$ifNull": ["$customer_phone", ""]}}, 0]}, "$customer_phone", "$conversation_id"]}}},
            {"$count": "count"}], "as": "demand"}},
        {"$set": {"asked": {"$ifNull": [{"$arrayElemAt": ["$demand.count", 0]}, 0]}}}]
    if requested_only:
        pipeline.append({"$match": {"asked": {"$gt": 0}}})
    pipeline += [{"$sort": {"asked": -1, "model_no": 1, "_id": 1}}, {"$facet": {
        "items": [{"$skip": (page - 1) * 50}, {"$limit": 50}, {"$project": {
            "model_no": 1, "unique_identifier": 1, "product_name": 1, "model_name": 1, "sku": 1,
            "asked": 1, "approved_floor_price": 1, "selling_gst_inclusive": 1, "gst_rate": 1,
            "price_version": 1, "price_source_sheet_id": 1, "price_verified_at": 1,
            "minimum_changed_at": 1, "minimum_publication": 1}}], "total": [{"$count": "count"}]}}]
    data = (await db[f"products_{catalog}"].aggregate(pipeline).to_list(1))[0]
    change_query = {"org_id": user.org_id, "kind": "daily_minimum", "status": {"$in": ["pending_approval", "publishing", "uncertain"]}}
    if user.role not in ADMIN:
        change_query["requested_by"] = user.user_id
    changes = await db.stock_change_requests.find(change_query).sort("created_at", -1).limit(50).to_list(50)
    organization = await db.organizations.find_one({"org_id": user.org_id}) or {}
    from services.pricing_decision_service import get_pricing_settings
    return {"items": data["items"], "total": (data["total"] or [{"count": 0}])[0]["count"],
            "page": page, "can_edit": catalog == user.org_id, "changes": changes,
            "workflow_enabled": bool(organization.get("sales_workflow_v2_enabled")),
            "b2b_pricing_enabled": bool(organization.get("b2b_pricing_enabled")),
            "pricing_settings": await get_pricing_settings(user.org_id)}


async def _audit(change, action, user, details=None):
    await get_async_mongo_db().stock_change_audit.insert_one({
        "audit_id": hashlib.sha256(f"{change['change_id']}:{action}".encode()).hexdigest(),
        "org_id": change["org_id"], "product_id": change["product_id"], "change_id": change["change_id"],
        "kind": "daily_minimum", "action": action, "actor_id": user.user_id, "actor_role": user.role,
        "created_at": now(), "details": details or {},
    })


async def request_minimum(user, row):
    if await catalogue_org(user.org_id) != user.org_id:
        raise HTTPException(403, "Minimums must be edited by the organisation that owns the catalogue")
    value = normalize_inr(row["minimum"], field="minimum")
    if value > 999999999:
        raise HTTPException(422, "Minimum is outside the supported range")
    db = get_async_mongo_db()
    change_id = hashlib.sha256(f"minimum:{user.org_id}:{user.user_id}:{row['request_id']}".encode()).hexdigest()
    old = await db.stock_change_requests.find_one({"_id": change_id})
    if old:
        if old["product_id"] != row["product_id"] or old["base_version"] != row["expected_version"] or old["changes"]["approved_floor_price"] != str(value):
            raise HTTPException(409, "This request ID was already used for another change")
        return old
    product = await product_by_id(user.org_id, row["product_id"])
    if not product:
        raise HTTPException(404, "Product not found")
    if product.get("price_version") != row["expected_version"]:
        raise HTTPException(409, "Minimum changed; refresh and review")
    if product.get("mrp") is not None and value > normalize_inr(product["mrp"]):
        raise HTTPException(422, "Minimum cannot exceed the product MRP")
    change = {"_id": change_id, "change_id": change_id, "org_id": user.org_id, "kind": "daily_minimum",
        "product_id": row["product_id"], "sku": str(product.get("unique_identifier") or product.get("model_no") or product.get("sku") or ""),
        "base_version": row["expected_version"], "before": {
            "approved_floor_price": product.get("approved_floor_price"), "price_version": product.get("price_version"),
            "gst_rate": product.get("gst_rate"), "selling_gst_inclusive": product.get("selling_gst_inclusive")},
        "price_source_sheet_id": product.get("price_source_sheet_id"), "product_type": product.get("type"),
        "changes": {"approved_floor_price": str(value)}, "status": "pending_approval",
        "requested_by": user.user_id, "requested_by_name": user.full_name or "Team member",
        "created_at": now(), "updated_at": now()}
    try:
        await db.stock_change_requests.insert_one(change)
    except DuplicateKeyError:
        return await db.stock_change_requests.find_one({"_id": change_id})
    await _audit(change, "requested", user)
    return await publish_minimum(user, change_id) if user.role in ADMIN else change


async def publish_minimum(user, change_id):
    if user.role not in ADMIN:
        raise HTTPException(403, "An administrator must publish minimum prices")
    db = get_async_mongo_db()
    change = await db.stock_change_requests.find_one_and_update({
        "_id": change_id, "org_id": user.org_id, "kind": "daily_minimum", "status": "pending_approval",
    }, {"$set": {"status": "publishing", "updated_at": now()}}, return_document=ReturnDocument.AFTER)
    if not change:
        raise HTTPException(409, "This change is no longer awaiting publication")
    if await catalogue_org(user.org_id) != user.org_id:
        await db.stock_change_requests.update_one({"_id": change_id}, {"$set": {"status": "failed", "error": "Catalogue ownership changed"}})
        raise HTTPException(403, "Catalogue ownership changed")
    product = await product_by_id(user.org_id, change["product_id"])
    if product and product.get("price_source_sheet_id") != change["price_source_sheet_id"]:
        await db.stock_change_requests.update_one({"_id": change_id}, {"$set": {"status": "failed", "error": "Minimum source changed; review the owning Sheet"}})
        raise HTTPException(409, "Minimum source changed; review the owning Sheet")
    held = await db[f"products_{user.org_id}"].find_one_and_update({
        "_id": (product or {}).get("_id"), "price_version": change["base_version"],
        "minimum_publication.status": {"$nin": list(HELD)},
    }, {"$set": {"minimum_publication": {"status": "publishing", "change_id": change_id, "started_at": now(), "write_attempted": False}}},
        return_document=ReturnDocument.AFTER)
    status, error, result = "failed", "Minimum changed or another publication is unresolved; refresh and review", {}
    if held:
        try:
            from services.product_sheet_sync import write_product_sheet_values
            result = await write_product_sheet_values(user.org_id, held, {
                "approved_floor_price": change["changes"]["approved_floor_price"], "_minimum_change_id": change_id})
            status, error = "completed", ""
        except Exception as exc:
            current = await product_by_id(user.org_id, change["product_id"]) or {}
            status = "uncertain" if (current.get("minimum_publication") or {}).get("write_attempted") else "failed"
            error = str(exc)[:400] if isinstance(exc, ValueError) else "Sheet verification failed. Refresh and verify before retrying."
        fields = {"minimum_publication.status": status, "minimum_publication.error": error}
        if status == "completed" and (held.get("approved_floor_price") is None or
                normalize_inr(change["changes"]["approved_floor_price"]) != normalize_inr(held["approved_floor_price"])):
            fields["minimum_changed_at"] = now()
        await db[f"products_{user.org_id}"].update_one({"_id": held["_id"], "minimum_publication.change_id": change_id}, {"$set": fields})
    await db.stock_change_requests.update_one({"_id": change_id}, {"$set": {
        "status": status, "error": error, "result": result, "updated_at": now(), "published_by": user.user_id}})
    await _audit(change, status, user, {"before": change["before"], "result": result})
    return await db.stock_change_requests.find_one({"_id": change_id})


async def resolve_minimum(user, change_id, action):
    if user.role not in ADMIN:
        raise HTTPException(403, "An administrator must review minimum changes")
    if action == "approve":
        return await publish_minimum(user, change_id)
    db = get_async_mongo_db()
    change = await db.stock_change_requests.find_one({"_id": change_id, "org_id": user.org_id, "kind": "daily_minimum"})
    if not change:
        raise HTTPException(404, "Change not found")
    if action == "reject":
        updated = await db.stock_change_requests.find_one_and_update({"_id": change_id, "status": "pending_approval"},
            {"$set": {"status": "rejected", "updated_at": now()}}, return_document=ReturnDocument.AFTER)
        if not updated:
            raise HTTPException(409, "Only an unpublished request can be rejected")
        await _audit(change, "rejected", user)
        return updated
    if change["status"] not in HELD:
        return change
    # Reconciliation reads the Sheet. It never repeats the uncertain write.
    product = await product_by_id(user.org_id, change["product_id"])
    if product and (product.get("minimum_publication") or {}).get("change_id") != change_id and change["status"] == "publishing":
        started = change.get("updated_at") or change["created_at"]
        if now() - started.replace(tzinfo=timezone.utc) < timedelta(minutes=3):
            raise HTTPException(409, "Publication is still running")
        # Recover a crash after the request claim but before the product hold.
        # Holding the product first fences a delayed original writer.
        product = await db[f"products_{user.org_id}"].find_one_and_update({
            "_id": product["_id"], "minimum_publication.status": {"$nin": list(HELD)},
        }, {"$set": {"minimum_publication": {"status": "uncertain", "change_id": change_id,
            "started_at": started, "write_attempted": False}}}, return_document=ReturnDocument.AFTER)
    if not product or (product.get("minimum_publication") or {}).get("change_id") != change_id:
        raise HTTPException(409, "Product publication no longer matches this request")
    if (await catalogue_org(user.org_id) != user.org_id or product.get("price_source_sheet_id") != change.get("price_source_sheet_id")
            or product.get("type") != change.get("product_type")):
        raise HTTPException(409, "The minimum's source or ownership changed; review the original Sheet")
    started = (product.get("minimum_publication") or {}).get("started_at")
    if change["status"] == "publishing" and started and now() - started.replace(tzinfo=timezone.utc) < timedelta(minutes=3):
        raise HTTPException(409, "Publication is still running")
    from services.product_sheet_sync import sync_product_sheet
    await sync_product_sheet(user.org_id, change["price_source_sheet_id"])
    current = await product_by_id(user.org_id, change["product_id"])
    if not current or not current.get("price_verified_at") or current["price_verified_at"].replace(tzinfo=timezone.utc) < started.replace(tzinfo=timezone.utc):
        raise HTTPException(409, "The minimum could not be verified from its source")
    if (current.get("source_valid") is False or current.get("price_source_sheet_id") != change["price_source_sheet_id"]
            or current.get("gst_rate") != change["before"]["gst_rate"]
            or current.get("selling_gst_inclusive") != change["before"]["selling_gst_inclusive"]):
        raise HTTPException(409, "The source or tax basis changed; review the Sheet before resolving this publication")
    matches = normalize_inr(current["approved_floor_price"]) == normalize_inr(change["changes"]["approved_floor_price"])
    status = "completed" if matches else "failed"
    result = {"approved_floor_price": current["approved_floor_price"], "price_version": current["price_version"]}
    await _audit(change, "reconciled", user, {"result": result, "requested_value_present": matches})
    await db[f"products_{user.org_id}"].update_one({"_id": current["_id"], "minimum_publication.change_id": change_id},
        {"$set": {"minimum_publication.status": status, "minimum_publication.error": "", **({"minimum_changed_at": now()} if matches else {})}})
    await db.stock_change_requests.update_one({"_id": change_id}, {"$set": {"status": status,
        "result": result, "updated_at": now(), "error": "" if matches else "Sheet contains a different minimum; review the current value before making a new change"}})
    return await db.stock_change_requests.find_one({"_id": change_id})
