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
from database import get_mongo_db
from loguru import logger
from services.websocket_service import broadcast_on_stats_update
from services.utils import name_search_prefixes, normalize_phone

db = get_mongo_db()

# Pre-compiled regex patterns for performance
EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')

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
    existing_contact = contacts_collection.find_one(unique_filter, {"name_locked_by_vcf": 1, "_id": 0})
    is_name_locked = existing_contact.get("name_locked_by_vcf", False) if existing_contact else False

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

        projection = {"_id": 0}

        cursor = contacts_collection.find({}, projection).batch_size(services.BATCH_SIZE)

        contacts = []
        for contact in cursor:
            contacts.append(contact)

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
    query: dict = {}
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
                for field in ("full_name", "phone_number", "email", "username", "company", "gstin", "tally.legal_name")
            ]
        }
        if query:
            query = {"$and": [query, search_query]}
        else:
            query = search_query

    projection = {
        "_id": 0,
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
        "tally": 1,
    }
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
        "contacts": contacts,
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

async def get_unread_counts_by_labels(org_id: str):
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
        
        pipeline = [
            # Only conversations with unread messages
            {"$match": {"unread_count": {"$gt": 0}}},
            # Join with contacts to get labels
            {
                "$lookup": {
                    "from": f"contacts_{org_id}",
                    "localField": "conversation_id",
                    "foreignField": "conversation_id",
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
    conversation_bulk_ops: list[UpdateOne] = []
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
                
                # Update conversation document so UI conversation list shows new name immediately
                conversation_bulk_ops.append(
                    UpdateOne(
                        {"conversation_id": conversation_id},
                        {"$set": {
                            "customer_name": full_name,
                            "customer_name_prefixes": name_search_prefixes(full_name),
                        }}
                    )
                )

            update_doc: dict = {
                "$set": set_fields,
                "$setOnInsert": contact_on_insert,
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

    # Execute conversation name updates if any VCF contacts were updated
    if conversation_bulk_ops:
        try:
            db[f"conversations_{org_id}"].bulk_write(conversation_bulk_ops, ordered=False)
        except Exception as e:
            logger.error(f"Error updating conversation names from VCF upload: {e}")

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
        query: dict = {"is_bulk_uploaded": True}

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
        cursor  = col.find(query, {"_id": 0}).skip(skip).limit(limit).sort("upload_metadata.uploaded_date", -1)
        contacts = list(cursor)

        return {"contacts": contacts, "total": total, "page": page, "limit": limit}
    except Exception as e:
        logger.error(f"get_uploaded_contacts_paginated error: {e}")
        return {"contacts": [], "total": 0, "page": page, "limit": limit}


async def get_upload_batches(org_id: str) -> list[dict]:
    """Return all upload batch summaries for an org."""
    try:
        batch_col = db[f"contact_upload_batches_{org_id}"]
        batches = list(
            batch_col.find({}, {"_id": 0}).sort("uploaded_date", -1)
        )
        return batches
    except Exception as e:
        logger.error(f"get_upload_batches error: {e}")
        return []


def delete_uploaded_contact(org_id: str, conversation_id: str) -> dict:
    """
    Delete or demote a single bulk-uploaded contact.

    - If the contact was created ONLY by a bulk upload (no whatsapp_id, no Instagram messaging)
      → permanently delete the document.
    - If it existed before (has whatsapp_id or instagram_id = real conversation activity)
      → only remove is_bulk_uploaded flag & upload_metadata; preserve the live record.
    """
    col = db[f"contacts_{org_id}"]
    contact = col.find_one({"conversation_id": conversation_id})

    if not contact:
        return {"deleted": False, "reason": "not_found"}

    # Heuristic: a "real" contact has a non-null whatsapp_id or instagram_id
    has_live_identity = bool(contact.get("whatsapp_id") or contact.get("instagram_id"))

    if has_live_identity:
        # Demote only — keep the record for the live chat
        col.update_one(
            {"conversation_id": conversation_id},
            {"$unset": {"is_bulk_uploaded": "", "upload_metadata": "", "name_locked_by_vcf": ""},
             "$set":   {"updated_at": datetime.now(timezone.utc)}}
        )
        return {"deleted": False, "demoted": True, "reason": "live_contact_preserved"}
    else:
        col.delete_one({"conversation_id": conversation_id})
        return {"deleted": True}


def delete_upload_batch(org_id: str, batch_id: str) -> dict:
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

    contacts_in_batch = list(col.find({"upload_metadata.batch_id": batch_id}))

    deleted_count = 0
    demoted_count = 0

    for contact in contacts_in_batch:
        conversation_id  = contact.get("conversation_id")
        has_live_identity = bool(contact.get("whatsapp_id") or contact.get("instagram_id"))

        if has_live_identity:
            col.update_one(
                {"conversation_id": conversation_id},
                {"$unset": {"is_bulk_uploaded": "", "upload_metadata": "", "name_locked_by_vcf": ""},
                 "$set":   {"updated_at": now}}
            )
            demoted_count += 1
        else:
            col.delete_one({"conversation_id": conversation_id})
            deleted_count += 1

    # Remove the batch record itself
    batch_col.delete_one({"batch_id": batch_id})
    logger.info(f"Batch {batch_id} rolled back: {deleted_count} deleted, {demoted_count} demoted")

    return {
        "batch_id":      batch_id,
        "deleted_count": deleted_count,
        "demoted_count": demoted_count,
    }
