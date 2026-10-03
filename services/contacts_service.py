import asyncio
from datetime import datetime, timezone
from io import BytesIO
import re
import uuid

from fastapi import BackgroundTasks, HTTPException
import pandas as pd
from bson import ObjectId
from pymongo import UpdateOne
from pymongo.errors import PyMongoError, BulkWriteError

# Internal Modules
from core.services import services
from database import get_mongo_db, get_mongo_client
from loguru import logger
from services.feature_flags import tally_enabled
from services.websocket_service import broadcast_on_stats_update, broadcast_org_ws
from services.utils import name_search_prefixes, normalize_phone, serialize_mongo
from services.whatsapp_connection_service import whatsapp_conversation_scope

db = get_mongo_db()

# Pre-compiled regex patterns for performance
EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')


def directory_contact(contact):
    result = dict(contact)
    contact_id = result.pop("_id", None)
    if contact_id is not None:
        result["contact_id"] = str(contact_id)
    return result


def save_directory_contact(org_id: str, values: dict, contact_id: str | None = None):
    collection = db[f"contacts_{org_id}"]
    fields = {key: value for key, value in values.items() if key in {"full_name", "phone_number", "email", "company", "country", "categories"}}
    if "full_name" in fields:
        fields["full_name"] = str(fields["full_name"] or "").strip()
        if not fields["full_name"]:
            raise HTTPException(422, "Contact name is required.")
    if fields.get("email") and not _validate_email(fields["email"]):
        raise HTTPException(422, "Enter a valid email address.")
    if "phone_number" in fields:
        phone = normalize_phone(fields["phone_number"] or "", default_country_code=get_org_default_country_code(org_id))
        if not re.fullmatch(r"\+?\d{10,15}", phone or ""):
            raise HTTPException(422, "Enter a valid phone number.")
        fields["phone_number"] = phone
    if "categories" in fields:
        fields["categories"] = list(dict.fromkeys(str(tag).strip() for tag in fields["categories"] if str(tag).strip()))

    def save(session):
        now = datetime.now(timezone.utc)
        existing = None
        if contact_id:
            if not ObjectId.is_valid(contact_id):
                raise HTTPException(404, "Contact not found.")
            existing = collection.find_one({"_id": ObjectId(contact_id), "deleted_at": {"$exists": False}}, session=session)
            if not existing:
                raise HTTPException(404, "Contact not found.")
            if fields.get("phone_number") != existing.get("phone_number") and "phone_number" in fields and (existing.get("conversation_id") or existing.get("whatsapp_id") or existing.get("instagram_id") or existing.get("tally")):
                raise HTTPException(409, "The messaging or accounting identity cannot be changed here.")
        elif not fields.get("full_name") or not fields.get("phone_number"):
            raise HTTPException(422, "Name and phone number are required.")
        if fields.get("phone_number"):
            other = collection.find_one({"phone_number": fields["phone_number"]}, session=session)
            if other and (not existing or other["_id"] != existing["_id"]):
                if not contact_id and other.get("deleted_at"):
                    existing = other
                else:
                    raise HTTPException(409, "This phone number already has a saved contact.")
        if existing and existing.get("platform") == "tally" and any(key in fields and fields[key] != existing.get(key) for key in ("full_name", "email", "company", "phone_number")):
            raise HTTPException(409, "Edit accounting-owned details in Tally.")
        fields["updated_at"] = now
        if "full_name" in fields:
            fields["name_locked_by_user"] = True
        if existing:
            result = collection.find_one_and_update({"_id": existing["_id"]}, {"$set": fields, "$unset": {"deleted_at": ""}}, return_document=True, session=session)
        else:
            result = {**fields, "platform": "manual", "created_at": now, "categories": fields.get("categories", []), "labels": []}
            result["_id"] = collection.insert_one(result, session=session).inserted_id
        if fields.get("full_name"):
            conversation_ids = list(dict.fromkeys([*(result.get("conversation_ids") or []), result.get("conversation_id")]))
            conversation_ids = [value for value in conversation_ids if value]
            if conversation_ids:
                db[f"conversations_{org_id}"].update_many({"conversation_id": {"$in": conversation_ids}}, {"$set": {"customer_name": fields["full_name"], "customer_name_prefixes": name_search_prefixes(fields["full_name"])}}, session=session)
        return directory_contact(result)
    with get_mongo_client().start_session() as session:
        return session.with_transaction(save)


def delete_directory_contact(org_id: str, contact_id: str):
    if not ObjectId.is_valid(contact_id):
        raise HTTPException(404, "Contact not found.")
    result = db[f"contacts_{org_id}"].update_one({"_id": ObjectId(contact_id)}, {"$set": {"deleted_at": datetime.now(timezone.utc)}})
    if not result.matched_count:
        raise HTTPException(404, "Contact not found.")
    return {"deleted": True}


def store_contact_background(background_tasks: BackgroundTasks, org_id: str, platform: str, contact_data: dict):
    """
    Trigger storing contact in the background.
    """
    background_tasks.add_task(store_contact, org_id, platform, contact_data)

def store_contact(org_id: str, platform: str, contact_data: dict):
    """
    Synchronously store or update a contact in the org-specific collection.
    """
    collection_name = f"contacts_{org_id}"

    contacts_collection = db[collection_name]

    now = datetime.now(timezone.utc)
    contact_data["platform"] = platform
    contact_data.setdefault("created_at", now)

    if platform == "instagram":
        unique_filter = {"instagram_id": contact_data["instagram_id"]}
    elif platform == "whatsapp":
        unique_filter = {"phone_number": contact_data["phone_number"]}
    else:
        logger.warning(f"Unknown platform {platform} - skipping contact storage.")
        return

    incoming_name = contact_data.get("full_name")

    # Check if the contact's name was locked by a VCF upload.
    # If so, we must NOT overwrite the name from a webhook/API call.
    existing_contact = contacts_collection.find_one(unique_filter, {"name_locked_by_vcf": 1, "name_locked_by_user": 1, "deleted_at": 1, "_id": 0})
    is_name_locked = bool(existing_contact and not existing_contact.get("deleted_at") and (existing_contact.get("name_locked_by_vcf") or existing_contact.get("name_locked_by_user")))

    set_on_insert_doc = {
        "created_at": now,
        "categories": contact_data.get("categories", []),
        "labels": contact_data.get("labels", []),
        "email": contact_data.get("email"),
        "country": contact_data.get("country"),
        "profile_url": contact_data.get("profile_url"),
    }

    # Only set name on insert if the name is NOT locked by VCF
    if not is_name_locked:
        set_on_insert_doc["full_name"] = incoming_name

    update_doc = {
        "$set": {
            "conversation_id": contact_data.get("conversation_id"),
            "platform": platform,
            "updated_at": now
        },
        "$setOnInsert": set_on_insert_doc,
    }

    # Add platform-specific fields
    if platform == "instagram":
        update_doc["$set"]["instagram_id"] = contact_data.get("instagram_id")
        update_doc["$set"]["username"] = contact_data.get("username")
    elif platform == "whatsapp":
        update_doc["$set"]["phone_number"] = contact_data.get("phone_number")
        update_doc["$set"]["whatsapp_id"] = contact_data.get("whatsapp_id")
        if contact_data.get("conversation_id"):
            update_doc["$addToSet"] = {"conversation_ids": contact_data["conversation_id"]}

    # Clean up $set to avoid overwriting with None if fields are missing in contact_data
    for key in list(update_doc["$set"].keys()):
        if update_doc["$set"][key] is None:
            del update_doc["$set"][key]

    contacts_collection.update_one(unique_filter, update_doc, upsert=True)
    logger.success(f"Stored/Updated contact for {platform}: {unique_filter}")
    
def _get_contacts_for_org(org_id: str):
    """
    Retrieve all contacts for a given organization.
    """
    try:
        collection_name = f"contacts_{org_id}"

        if collection_name not in db.list_collection_names():
            logger.info(f"Collection {collection_name} does not exist.")
            return []

        contacts_collection = db[collection_name]

        include_tally = tally_enabled(org_id)
        query = {"deleted_at": {"$exists": False}}
        if not include_tally:
            query["platform"] = {"$ne": "tally"}
        projection = None if include_tally else {"tally": 0}

        cursor = contacts_collection.find(query, projection).batch_size(services.BATCH_SIZE)

        contacts = []
        for contact in cursor:
            contacts.append(directory_contact(contact))

        logger.info(f"Retrieved {len(contacts)} contacts for org {org_id}.")
        return contacts

    except PyMongoError as e:
        logger.error(f"MongoDB error while fetching contacts for {org_id}: {e}")
        return []

    except Exception as e:
        logger.error(f"Unexpected error in get_contacts_for_org({org_id}): {e}")
        return []


def _get_contacts_page(
    org_id: str,
    page: int,
    limit: int,
    search: str,
    tags: list[str],
    snapshot: str | None,
) -> dict:
    collection = db[f"contacts_{org_id}"]
    include_tally = tally_enabled(org_id)
    query: dict = {"deleted_at": {"$exists": False}}
    if not include_tally:
        query["platform"] = {"$ne": "tally"}
    tags = [str(tag).strip()[:100] for tag in tags[:20] if str(tag).strip()]

    if tags:
        query["$or"] = [
            {"categories": {"$in": tags}},
            {"labels": {"$in": tags}},
        ]

    safe_search = str(search)[:100].strip()
    if safe_search:
        # ponytail: escaped scans are limited to explicit searches; move to Atlas Search past 100k contacts/org.
        search_query = {
            "$or": [
                {field: {"$regex": re.escape(safe_search), "$options": "i"}}
                for field in (
                    "full_name", "phone_number", "email", "username", "company", "gstin",
                    *(("tally.legal_name",) if include_tally else ()),
                )
            ]
        }
        if query:
            query = {"$and": [query, search_query]}
        else:
            query = search_query

    projection = {
        "_id": 1,
        "platform": 1,
        "full_name": 1,
        "country": 1,
        "profile_url": 1,
        "conversation_id": 1,
        "email": 1,
        "categories": 1,
        "labels": 1,
        "phone_number": 1,
        "instagram_id": 1,
        "username": 1,
        "company": 1,
        "gstin": 1,
    }
    if include_tally:
        projection["tally"] = 1
    if snapshot:
        if not ObjectId.is_valid(snapshot):
            raise HTTPException(status_code=422, detail="Invalid contacts snapshot")
        snapshot_id = ObjectId(snapshot)
    else:
        newest = collection.find_one(query, {"_id": 1}, sort=[("_id", -1)])
        snapshot_id = newest.get("_id") if newest else None

    if snapshot_id:
        query = {"$and": [query, {"_id": {"$lte": snapshot_id}}]} if query else {"_id": {"$lte": snapshot_id}}

    total = collection.count_documents(query)
    contacts = list(
        collection.find(query, projection)
        .sort("_id", -1)
        .skip((page - 1) * limit)
        .limit(limit)
    )
    return {
        "contacts": [directory_contact(contact) for contact in contacts],
        "total": total,
        "page": page,
        "limit": limit,
        "snapshot": str(snapshot_id) if snapshot_id else None,
    }


async def get_contacts_for_org(org_id: str):
    """Retrieve all contacts for legacy clients without blocking the async loop."""
    return await asyncio.to_thread(_get_contacts_for_org, org_id)


async def get_contacts_page_for_org(
    org_id: str,
    page: int,
    limit: int,
    search: str = "",
    tags: list[str] | None = None,
    snapshot: str | None = None,
) -> dict:
    """Return one contact page without blocking the async request loop."""
    try:
        return await asyncio.to_thread(
            _get_contacts_page,
            org_id,
            page,
            limit,
            search,
            tags or [],
            snapshot,
        )
    except PyMongoError as error:
        logger.error(f"MongoDB error while fetching a contact page: {type(error).__name__}")
        raise HTTPException(status_code=500, detail="Contacts could not be loaded") from error


async def update_contact_tag_categories(org_id: str, conversation_id: str, categories: list):
    """
    Update the categories array for a specific contact by conversation_id.
    """
    try:
        collection_name = f"contacts_{org_id}"

        if collection_name not in db.list_collection_names():
            raise HTTPException(status_code=404, detail=f"No contacts found for org {org_id}")

        contacts_collection = db[collection_name]

        result = contacts_collection.find_one_and_update(
            {"conversation_id": conversation_id},
            {"$set": {"categories": categories}},
            return_document=True
        )

        if not result:
            raise HTTPException(status_code=404, detail="Contact not found")

        # Live Update: Trigger dashboard refresh
        # from services.websocket_service import broadcast_on_stats_update
        # import asyncio
        # asyncio.create_task(broadcast_on_stats_update(org_id))
        
        if "_id" in result:
            del result["_id"]

        return {"contact": result}

    except PyMongoError as e:
        logger.error(f"MongoDB error while updating categories for {org_id}: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred")

    except Exception as e:
        logger.error(f"Unexpected error in update_contact_categories: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error occurred")

async def update_contact_labels(org_id: str, conversation_id: str, labels: list):
    """
    Update the labels array for a specific contact by conversation_id.
    """
    try:
        collection_name = f"contacts_{org_id}"

        if collection_name not in db.list_collection_names():
            raise HTTPException(status_code=404, detail=f"No contacts found for org {org_id}")

        contacts_collection = db[collection_name]

        result = contacts_collection.find_one_and_update(
            {"conversation_id": conversation_id},
            {"$set": {"labels": labels}},
            return_document=True
        )

        if not result:
            raise HTTPException(status_code=404, detail="Contact not found")
        
        if "_id" in result:
            del result["_id"]

        # await broadcast_on_stats_update(org_id)
        return {"contact": result}

    except PyMongoError as e:
        logger.error(f"MongoDB error while updating labels for {org_id}: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred")

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Unexpected error in update_contact_labels: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error occurred")
    
async def add_tag_to_org_metadata(org_id: str, tag: str):
    """Add a tag to the organization's metadata."""

    metadata = db.organizations_metadata
    result = metadata.update_one(
        {"org_id": org_id},
        {"$addToSet": {"tags": tag}},
        upsert=True
    )
    
    return result.modified_count > 0 or result.upserted_id is not None

async def add_label_to_org_metadata(org_id: str, label: str):
    """Add a label to the organization's metadata."""

    metadata = db.organizations_metadata
    result = metadata.update_one(
        {"org_id": org_id},
        {"$addToSet": {"labels": label}},
        upsert=True
    )
    
    return result.modified_count > 0 or result.upserted_id is not None
    
async def remove_tag_from_org_metadata(org_id: str, tag: str):
    """Remove a tag from the organization's metadata."""

    metadata = db.organizations_metadata
    metadata.update_one(
        {"org_id": org_id},
        {"$pull": {"tags": tag}}
    )

    return True

async def remove_label_from_org_metadata(org_id: str, label: str):
    """Remove a label from the organization's metadata."""

    metadata = db.organizations_metadata
    metadata.update_one(
        {"org_id": org_id},
        {"$pull": {"labels": label}}
    )

    return True

async def get_tags_from_org_metadata(org_id: str):
    """Get tags for the organization."""

    metadata = db.organizations_metadata
    org_metadata = metadata.find_one({"org_id": org_id}, {"_id": 0, "tags": 1})

    if org_metadata and "tags" in org_metadata:
        return org_metadata["tags"]
    else:
        return []

async def get_labels_from_org_metadata(org_id: str):
    """Get labels for the organization."""

    metadata = db.organizations_metadata
    org_metadata = metadata.find_one({"org_id": org_id}, {"_id": 0, "labels": 1})

    if org_metadata and "labels" in org_metadata:
        return org_metadata["labels"]
    else:
        return []

async def get_unread_counts_by_labels(org_id: str, business_phone_id: str | None = None):
    """
    Get labels from org metadata and calculate unread conversation count for each label.
    Returns a dictionary: { "label_name": unread_count, ... }
    """
    try:
        # 1. Get all labels defined for this org
        all_labels = await get_labels_from_org_metadata(org_id)
        
        # Initialize results with 0 for all known labels
        result_map = {label: 0 for label in all_labels}
        
        # 2. Aggregate unread counts from conversations joined with contacts
        # We count conversations where unread_count > 0
        from database import get_async_mongo_db
        async_db = get_async_mongo_db()
        
        conv_col = async_db[f"conversations_{org_id}"]
        
        conversation_match = {"unread_count": {"$gt": 0}}
        if business_phone_id:
            active = {"org_id": org_id, "is_active": {"$ne": False}}
            primary = await async_db.whatsapp_connections.find_one(
                {**active, "slot": 1}, {"phone_id": 1}
            ) or await async_db.whatsapp_connections.find_one(
                active, {"phone_id": 1}, sort=[("connected_at", 1)]
            )
            conversation_match.update(whatsapp_conversation_scope(
                business_phone_id,
                include_legacy=bool(primary and str(primary.get("phone_id") or "") == str(business_phone_id)),
            ))
        pipeline = [
            # Only conversations with unread messages
            {"$match": conversation_match},
            # Join with contacts to get labels
            {
                "$lookup": {
                    "from": f"contacts_{org_id}",
                    "let": {"conversation_id": "$conversation_id"},
                    "pipeline": [{"$match": {"$expr": {"$or": [
                        {"$eq": ["$conversation_id", "$$conversation_id"]},
                        {"$in": ["$$conversation_id", {"$ifNull": ["$conversation_ids", []]}]},
                    ]}}}],
                    "as": "contact"
                }
            },
            # Only keep those that have a corresponding contact
            {"$match": {"contact": {"$ne": []}}},
            # Unwind the contact array
            {"$unwind": "$contact"},
            # Unwind the labels array within the contact
            {"$unwind": "$contact.labels"},
            # Group by label and count conversations
            {
                "$group": {
                    "_id": "$contact.labels",
                    "unread_count": {"$sum": 1}
                }
            }
        ]
        
        cursor = conv_col.aggregate(pipeline)
        async for doc in cursor:
            label_name = doc["_id"]
            count = doc["unread_count"]
            # Strictly only include labels that exist in org metadata
            if label_name in result_map:
                result_map[label_name] = count
                
        return result_map


    except Exception as e:
        logger.error(f"Error in get_unread_counts_by_labels for {org_id}: {e}")
        return {}




def _validate_email(email: str) -> bool:
    """Simple but robust email format check."""
    return bool(EMAIL_PATTERN.match(email.strip()))


def get_org_default_country_code(org_id: str) -> str:
    """
    Retrieve the organization's connected phone's country code prefix.
    If none is found or it's invalid, fallback to '91'.
    """
    try:
        connection = db.whatsapp_connections.find_one({"org_id": org_id, "is_active": True})
        if not connection:
            connection = db.whatsapp_connections.find_one({"org_id": org_id})
        
        if connection and connection.get("phone"):
            phone = str(connection["phone"]).strip().lstrip("+")
            if len(phone) > 10:
                return phone[:-10]
    except Exception as e:
        logger.error(f"Error fetching default country code for org {org_id}: {e}")
    return "91"


def _vcf_contact_sync_plan(contact: dict, conversations: list[dict]) -> dict:
    ids = sorted({row["conversation_id"] for row in conversations})
    contact_changes = {}
    if ids:
        def activity(row):
            value = row.get("last_message_timestamp")
            if isinstance(value, datetime):
                value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
                return value.timestamp(), row["conversation_id"]
            return float("-inf"), row["conversation_id"]

        selected = contact.get("conversation_id")
        if selected not in ids:
            selected = max(conversations, key=activity)["conversation_id"]
        for key, value in {"conversation_id": selected, "conversation_ids": ids}.items():
            if contact.get(key) != value:
                contact_changes[key] = value

    names = {"customer_name": contact["full_name"], "customer_name_prefixes": name_search_prefixes(contact["full_name"])}
    changes = [
        {"conversation_id": row["conversation_id"], "before": {key: row.get(key) for key in names}, "after": names}
        for row in conversations if any(row.get(key) != value for key, value in names.items())
    ]
    if contact_changes or changes:
        contact_changes["updated_at"] = datetime.now(timezone.utc)
    return {
        "contact_id": str(contact["_id"]), "phone_number": contact["phone_number"],
        "contact_before": {key: contact.get(key) for key in contact_changes},
        "contact_after": contact_changes, "conversations": changes,
        "conversation_ids": ids,
    }


def sync_vcf_contacts_for_org(org_id: str, *, batch_id: str | None = None, apply: bool = False) -> dict:
    """Plan or apply contact-name/link repairs; each applied contact is transactional."""
    contacts = db[f"contacts_{org_id}"]
    conversations = db[f"conversations_{org_id}"]
    query = {"name_locked_by_vcf": True, "deleted_at": {"$exists": False}}
    if batch_id:
        query["upload_metadata.batch_id"] = batch_id
    report = {"changes": [], "errors": [], "updated_conversation_ids": []}
    candidates = list(contacts.find(query))

    for candidate in candidates:
        def sync(session=None):
            guard = {
                "_id": candidate["_id"], "phone_number": candidate.get("phone_number"),
                "full_name": candidate.get("full_name"), "updated_at": candidate.get("updated_at"),
                "name_locked_by_vcf": True, "deleted_at": {"$exists": False},
            }
            current = contacts.find_one(guard, session=session)
            if not current or not current.get("full_name"):
                raise ValueError("Contact changed or has no saved name; rerun the repair.")
            duplicates = contacts.find({"phone_number": current["phone_number"], "deleted_at": {"$exists": False}}, session=session)
            if any(row.get("full_name") != current["full_name"] for row in duplicates):
                raise ValueError("Duplicate contacts have conflicting names; resolve them before repair.")
            phone = normalize_phone(current["phone_number"], is_canonical=True)
            if not phone:
                raise ValueError("Contact has no valid customer phone.")
            rows = list(conversations.find({"platform": "whatsapp", "customer_id": phone}, session=session))
            plan = _vcf_contact_sync_plan(current, rows)
            if apply and (plan["contact_after"] or plan["conversations"]):
                # A real contact write holds the transaction lock against concurrent name edits.
                result = contacts.update_one(guard, {"$set": plan["contact_after"]}, session=session)
                if result.matched_count != 1:
                    raise ValueError("Contact changed; rerun the repair.")
                changes = {change["conversation_id"]: change["after"] for change in plan["conversations"]}
                operations = [
                    UpdateOne({"_id": row["_id"], "platform": "whatsapp", "customer_id": phone}, {"$set": changes[row["conversation_id"]]})
                    for row in rows if row["conversation_id"] in changes
                ]
                if operations:
                    result = conversations.bulk_write(operations, ordered=False, session=session)
                    if result.matched_count != len(operations):
                        raise ValueError("Conversation changed; rerun the repair.")
            return plan

        try:
            if apply:
                # ponytail: per-contact transactions; batch transactions if large imports dominate latency.
                with get_mongo_client().start_session() as session:
                    plan = session.with_transaction(sync)
            else:
                plan = sync()
            if plan["contact_after"] or plan["conversations"]:
                report["changes"].append(plan)
                if apply:
                    report["updated_conversation_ids"].extend(plan["conversation_ids"])
        except Exception as error:
            logger.error(f"VCF inbox sync failed for contact {candidate['_id']}: {error}")
            report["errors"].append({"contact_id": str(candidate["_id"]), "reason": str(error)})

    report["updated_conversation_ids"] = sorted(set(report["updated_conversation_ids"]))
    return report


async def broadcast_vcf_contact_sync(org_id: str, conversation_ids: list[str]):
    if not conversation_ids:
        return
    try:
        rows = await asyncio.to_thread(lambda: list(db[f"conversations_{org_id}"].find({"conversation_id": {"$in": conversation_ids}}, {"_id": 0})))
        for row in rows:
            await broadcast_org_ws(org_id, "conversation_updated", {
                "conversation_id": row["conversation_id"], "reason": "contact_name_updated",
                "conversation": serialize_mongo(row),
            })
    except Exception as error:
        logger.error(f"VCF inbox notification failed for org {org_id}: {error}")


def process_bulk_wa_contact_upload(
    org_id: str,
    file_content: bytes,
    filename: str,
    uploaded_by: str = "unknown",
    column_mapping: dict | None = None,
) -> dict:
    """
    Enhanced bulk contact importer.

    Supports: .csv, .xlsx, .xls, .vcf
    - column_mapping: optional dict mapping system field names to actual file column headers.
      e.g. {"name": "Full Name", "phone": "Mobile", "email": "Email Address", "company": "Organization"}
    - Stores batch metadata in contact_upload_batches_{org_id}.
    - Marks every processed contact with is_bulk_uploaded=True + upload_metadata.
    - Pre-existing contacts (from live chats/webhooks) are merged safely — user edits are preserved.
    """
    from services.vcard_parser import parse_vcf

    fname_lower = (filename or "").lower()
    now = datetime.now(timezone.utc)
    batch_id = str(uuid.uuid4())
    if fname_lower.endswith(".vcf"):
        source_type = "vcf"
    elif fname_lower.endswith(".csv"):
        source_type = "csv"
    elif fname_lower.endswith(".xls") or fname_lower.endswith(".xlsx"):
        source_type = "excel"
    else:
        raise ValueError(
        f"Unsupported file format for '{filename}'. Supported formats are: .csv, .xls, .xlsx, .vcf"
            )

    # ── 1. Parse file into a unified list of raw dicts ───────────────────────
    raw_contacts: list[dict] = []

    try:
        if source_type == "vcf":
            raw_contacts = parse_vcf(file_content)
        elif source_type == "csv":
            df = pd.read_csv(BytesIO(file_content), dtype=str)
            df.columns = [c.strip() for c in df.columns]
            raw_contacts = df.where(pd.notna(df), None).to_dict(orient="records")
        else:
            df = pd.read_excel(BytesIO(file_content), dtype=str)
            df.columns = [c.strip() for c in df.columns]
            raw_contacts = df.where(pd.notna(df), None).to_dict(orient="records")
    except Exception as e:
        raise ValueError(f"Could not read file '{filename}': {e}")

    if not raw_contacts:
        raise ValueError("The uploaded file is empty or contains no parseable records.")

    # ── 2. Build column resolver based on mapping or auto-detect ─────────────
    def _resolve(row: dict, system_key: str, fallbacks: list[str]) -> str | None:
        """Return the value for a system field from a row dict, respecting mapping."""
        resolved_key = "full_name" if system_key == "name" else system_key
        if column_mapping:
            mapped_col = column_mapping.get(resolved_key) or column_mapping.get(system_key)
            if mapped_col:
                val = row.get(mapped_col)
                return str(val).strip() if val is not None else None

        # VCF parser already returns clean keys
        if system_key in row:
            val = row[system_key]
            return str(val).strip() if val is not None else None

        # Auto-detect: try case-insensitive fallback column names
        for key in fallbacks:
            for col, val in row.items():
                if col.lower() == key.lower() and val is not None:
                    return str(val).strip()
        return None

    # ── 3. Process each record ────────────────────────────────────────────────
    contacts_collection = db[f"contacts_{org_id}"]
    default_cc = get_org_default_country_code(org_id)

    # Pre-gather all phone numbers from the file to batch-query existing ones for O(1) deduplication
    uploaded_phones = set()
    for row in raw_contacts:
        raw_phone = _resolve(row, "phone", ["phone", "mobile", "tel", "telephone", "phone number", "contact"])
        clean_phone = normalize_phone(raw_phone or "", default_country_code=default_cc)
        if clean_phone:
            uploaded_phones.add(clean_phone)

    existing_phones = set()
    if uploaded_phones:
        try:
            cursor = contacts_collection.find(
                {"phone_number": {"$in": list(uploaded_phones)}},
                {"phone_number": 1, "_id": 0}
            )
            existing_phones = {doc["phone_number"] for doc in cursor if doc.get("phone_number")}
        except Exception as e:
            logger.error(f"Error querying existing contacts for deduplication: {e}")

    processed_in_batch = set()
    bulk_ops: list[UpdateOne] = []
    skipped_records: list[dict] = []
    success_count = 0

    idx = 0
    for row in raw_contacts:
        idx += 1
        # Resolve fields
        full_name = _resolve(row, "full_name", ["name", "full name", "fullname", "fn"])
        
        # Clean duplicated names like Name | Name or Name | Company from sync tools
        if full_name and "|" in full_name:
            full_name = full_name.split("|")[0].strip()

        raw_phone_str = _resolve(row, "phone", ["phone", "mobile", "tel", "telephone", "phone number", "contact"])
        email_raw = _resolve(row, "email", ["email", "email address", "e-mail"])
        company   = _resolve(row, "company", ["company", "org", "organisation", "organization", "employer"])

        # Support multiple phone numbers separated by comma or semicolon
        phone_list = []
        if raw_phone_str:
            phone_list = [p.strip() for p in re.split(r'[,;]+', str(raw_phone_str)) if p.strip()]
        
        if not phone_list:
            phone_list = [""]

        for raw_phone in phone_list:
            # Normalise phone
            clean_phone = normalize_phone(raw_phone, default_country_code=default_cc)

            # Validate — phone is required, name optional but noted
            if not clean_phone or len(clean_phone.lstrip("+")) < 10:
                skipped_records.append({
                    "row_number": idx,
                    "value": raw_phone,
                    "reason": "Missing or invalid phone number (must have ≥ 10 digits)"
                })
                continue

            # Skip if duplicate within the same uploaded file
            if clean_phone in processed_in_batch:
                skipped_records.append({
                    "row_number": idx,
                    "value": raw_phone,
                    "reason": f"Duplicate phone number in the uploaded file (phone: {clean_phone})"
                })
                continue

            processed_in_batch.add(clean_phone)

            if not full_name:
                full_name = f"Contact {clean_phone[-4:]}"  # fallback display name

            # Validate email if present
            email: str | None = None
            if email_raw and _validate_email(email_raw):
                email = email_raw.lower()
            elif email_raw:
                # still continue — just skip the bad email
                skipped_records.append({
                    "row_number": idx,
                    "value": email_raw,
                    "reason": f"Invalid email format for phone {clean_phone} — email skipped, contact imported"
                })

            conversation_id = f"whatsapp_{clean_phone}"
            upload_metadata = {
                "batch_id": batch_id,
                "source_type": source_type,
                "uploaded_by": uploaded_by,
                "uploaded_date": now,
                "filename": filename,
            }

            # Sets all base fields for new records
            contact_on_insert = {
                "full_name": full_name,
                "phone_number": clean_phone,
                "whatsapp_id": None,
                "country": None,
                "profile_url": None,
                "conversation_id": conversation_id,
                "categories": [],
                "labels": [],
                "platform": "whatsapp",
                "created_at": now,
            }
            if email:
                contact_on_insert["email"] = email
            if company:
                contact_on_insert["company"] = company

            set_fields: dict = {
                "is_bulk_uploaded": True,
                "upload_metadata": upload_metadata,
                "updated_at": now,
            }

            # For VCF uploads: force-update the contact name on existing records
            # and lock it so WhatsApp API profile-name webhooks don't overwrite it.
            if source_type == "vcf" and full_name:
                set_fields["full_name"] = full_name
                set_fields["name_locked_by_vcf"] = True
                # Remove full_name from $setOnInsert to avoid MongoDB conflict
                # (same field cannot be in both $set and $setOnInsert)
                contact_on_insert.pop("full_name", None)
                
            update_doc: dict = {
                "$set": set_fields,
                "$setOnInsert": contact_on_insert,
                "$unset": {"deleted_at": ""},
            }

            bulk_ops.append(
                UpdateOne(
                    {"phone_number": clean_phone},
                    update_doc,
                    upsert=True,
                )
            )

    # ── 4. Execute batch write ────────────────────────────────────────────────
    newly_added = 0
    matched_count = 0
    if bulk_ops:
        try:
            result = contacts_collection.bulk_write(bulk_ops, ordered=False)
            newly_added   = result.upserted_count
            matched_count = result.matched_count
        except BulkWriteError as bwe:
            # Catch write errors to prevent API crashes and capture partial successes
            details = bwe.details
            newly_added   = details.get("nUpserted", 0)
            matched_count = details.get("nMatched", 0)
            
            write_errors = details.get("writeErrors", [])
            for err in write_errors:
                logger.error(f"Bulk write error details: {err}")
                skipped_records.append({
                    "row_number": "N/A",
                    "value": f"Record Index {err.get('index')}",
                    "reason": f"Database write failure: {err.get('errmsg', 'Unknown database error')}"
                })
        
        success_count = newly_added + matched_count

    sync_report = {"errors": [], "updated_conversation_ids": []}
    if source_type == "vcf" and success_count:
        try:
            # Batch metadata selects only contacts whose upload write actually succeeded.
            sync_report = sync_vcf_contacts_for_org(org_id, batch_id=batch_id, apply=True)
        except Exception as error:
            logger.error(f"VCF inbox synchronization failed for batch {batch_id}: {error}")
            sync_report["errors"] = [{"reason": str(error)}] * success_count

    # ── 5. Persist batch metadata ─────────────────────────────────────────────
    batch_doc = {
        "batch_id": batch_id,
        "filename": filename,
        "source_type": source_type,
        "uploaded_by": uploaded_by,
        "uploaded_date": now,
        "total_records": len(raw_contacts),
        "success_count": success_count,
        "failed_count": len(skipped_records),
        "newly_added": newly_added,
        "merged_existing": matched_count,
    }
    db[f"contact_upload_batches_{org_id}"].insert_one(batch_doc)
    logger.success(f"Bulk upload batch {batch_id} complete — {newly_added} new, {matched_count} merged, {len(skipped_records)} skipped")

    return {
        "batch_id": batch_id,
        "total_rows_in_file": len(raw_contacts),
        "successfully_processed": success_count,
        "newly_added": newly_added,
        "merged_existing": matched_count,
        "skipped_invalid_rows": len(skipped_records),
        "skipped_records": skipped_records,
        **({"inbox_sync": {
            "status": "partial" if sync_report["errors"] else "synced",
            "failed_contacts": len(sync_report["errors"]),
        }} if source_type == "vcf" else {}),
        "_updated_conversation_ids": sync_report["updated_conversation_ids"],
    }


async def get_uploaded_contacts_paginated(
    org_id: str,
    page: int = 1,
    limit: int = 25,
    search: str = "",
    batch_id: str | None = None,
) -> dict:
    """Return paginated list of bulk-uploaded contacts (is_bulk_uploaded=True)."""
    try:
        collection_name = f"contacts_{org_id}"
        if collection_name not in db.list_collection_names():
            return {"contacts": [], "total": 0, "page": page, "limit": limit}

        col = db[collection_name]
        query: dict = {"is_bulk_uploaded": True, "deleted_at": {"$exists": False}}

        if batch_id:
            query["upload_metadata.batch_id"] = batch_id

        if search:
            # Enforce a max length of 100 characters to prevent parsing issues
            safe_search = str(search)[:100].strip()
            # Escape special regex characters to prevent regex injection (ReDoS) and literal matching
            escaped_search = re.escape(safe_search)

            query["$or"] = [
                {"full_name":    {"$regex": escaped_search, "$options": "i"}},
                {"phone_number": {"$regex": escaped_search, "$options": "i"}},
                {"email":        {"$regex": escaped_search, "$options": "i"}},
                {"company":      {"$regex": escaped_search, "$options": "i"}},
            ]

        total   = col.count_documents(query)
        skip    = (page - 1) * limit
        cursor  = col.find(query).skip(skip).limit(limit).sort("upload_metadata.uploaded_date", -1)
        contacts = list(cursor)
        for contact in contacts:
            contact["record_id"] = str(contact.pop("_id"))
            contact["contact_id"] = contact["record_id"]

        return {"contacts": contacts, "total": total, "page": page, "limit": limit}
    except Exception as e:
        logger.error(f"get_uploaded_contacts_paginated error: {e}")
        return {"contacts": [], "total": 0, "page": page, "limit": limit}


async def get_upload_batches(org_id: str) -> list[dict]:
    """Return all upload batch summaries for an org."""
    try:
        batch_col = db[f"contact_upload_batches_{org_id}"]
        batches = list(
            batch_col.find({}).sort("uploaded_date", -1)
        )
        for batch in batches:
            batch["record_id"] = str(batch.pop("_id"))
        return batches
    except Exception as e:
        logger.error(f"get_upload_batches error: {e}")
        return []


def delete_uploaded_contact(org_id: str, conversation_id: str) -> dict:
    query = {"_id": ObjectId(conversation_id)} if ObjectId.is_valid(conversation_id) else {"conversation_id": conversation_id}
    contact = db[f"contacts_{org_id}"].find_one(query)
    if not contact:
        return {"deleted": False, "reason": "not_found"}
    return delete_directory_contact(org_id, str(contact["_id"]))


def delete_upload_batch(org_id: str, identifier: str) -> dict:
    """
    Rollback an entire upload batch.

    For each contact in the batch:
    - If it was exclusively created by this batch → delete permanently.
    - If it was a pre-existing contact that got merged → demote only.
    Returns a summary dict.
    """
    col        = db[f"contacts_{org_id}"]
    batch_col  = db[f"contact_upload_batches_{org_id}"]
    now        = datetime.now(timezone.utc)

    batch = None
    if ObjectId.is_valid(identifier):
        batch = batch_col.find_one({"_id": ObjectId(identifier)})
    if not batch:
        batch = batch_col.find_one({"batch_id": identifier})
    if not batch:
        return {"deleted_count": 0, "demoted_count": 0, "reason": "not_found"}

    batch_id = batch.get("batch_id")
    if not batch_id:
        return {"deleted_count": 0, "demoted_count": 0, "reason": "invalid_batch"}

    contacts_in_batch = list(col.find({"is_bulk_uploaded": True, "upload_metadata.batch_id": batch_id}))

    deleted_count = 0
    demoted_count = 0

    for contact in contacts_in_batch:
        if contact.get("deleted_at"):
            continue
        has_live_identity = bool(contact.get("whatsapp_id") or contact.get("instagram_id"))
        selector = {"_id": contact["_id"], "is_bulk_uploaded": True}

        if has_live_identity:
            result = col.update_one(
                selector,
                {"$unset": {"is_bulk_uploaded": "", "upload_metadata": "", "name_locked_by_vcf": ""},
                 "$set":   {"updated_at": now}}
            )
            demoted_count += result.modified_count
        else:
            result = col.delete_one(selector)
            deleted_count += result.deleted_count

    # Remove the batch record itself
    batch_col.delete_one({"_id": batch["_id"]})
    logger.info(f"Batch {batch_id} rolled back: {deleted_count} deleted, {demoted_count} demoted")

    return {
        "batch_id":      batch_id,
        "deleted_count": deleted_count,
        "demoted_count": demoted_count,
    }
