from datetime import datetime, timezone
from fastapi import BackgroundTasks, HTTPException
from pymongo.errors import PyMongoError
import pandas as pd
from io import BytesIO
from pymongo import UpdateOne

# Internal Modules
from core.services import services
from database import get_mongo_db
from core.logger import get_logger

logger = get_logger(__name__)

db = get_mongo_db()

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

    if contacts_collection.find_one(unique_filter):
        logger.info(f"Contact already exists for {platform}: {unique_filter}")
        return
    
    contacts_collection.insert_one(contact_data)
    logger.success(f"Added new contact for {platform}: {unique_filter}")
    
async def get_contacts_for_org(org_id: str):
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
        
        if "_id" in result:
            del result["_id"]

        return {"contact": result}

    except PyMongoError as e:
        logger.error(f"MongoDB error while updating categories for {org_id}: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred")

    except Exception as e:
        logger.error(f"Unexpected error in update_contact_categories: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error occurred")
    
async def add_tag_to_org_metadata(org_id: str, tag: str):
    """Add a tag to the organization's metadata."""

    metadata = db.organizations_metadata
    result = metadata.update_one(
        {"org_id": org_id},
        {"$addToSet": {"tags": tag}},
        upsert=True
    )
    
    return result.modified_count > 0

async def remove_tag_from_org_metadata(org_id: str, tag: str):
    """Remove a tag from the organization's metadata."""

    metadata = db.organizations_metadata
    metadata.update_one(
        {"org_id": org_id},
        {"$pull": {"tags": tag}}
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

def process_bulk_wa_contact_upload(org_id: str, file_content: bytes, filename: str):
    try:
        if filename.endswith('.csv'):
            df = pd.read_csv(BytesIO(file_content))
        elif filename.endswith(('.xls', '.xlsx')):
            df = pd.read_excel(BytesIO(file_content))
        else:
            raise ValueError("Unsupported file format. Please upload CSV or Excel.")
    except Exception as e:
        raise ValueError(f"Could not read file: {str(e)}")

    df.columns = [c.strip().lower() for c in df.columns]
    required_columns = {'name', 'phone'}
    
    if not required_columns.issubset(set(df.columns)):
        missing = required_columns - set(df.columns)
        raise ValueError(f"Invalid format: Missing required columns: {', '.join(missing)}")

    if df.empty:
        raise ValueError("The uploaded file is empty.")

    collection_name = f"contacts_{org_id}"
    contacts_collection = db[collection_name]
    bulk_ops = []
    now = datetime.now(timezone.utc)
    
    skipped_rows = 0

    for index, row in df.iterrows():
        full_name = str(row['name']).strip() if pd.notna(row['name']) else None
        
        raw_phone = str(row['phone']) if pd.notna(row['phone']) else ""
        clean_phone = "".join(filter(str.isdigit, raw_phone))

        if not full_name or not clean_phone or len(clean_phone) < 10:
            skipped_rows += 1
            continue

        

        contact_doc = {
            "whatsapp_id": None,
            "full_name": full_name,
            "phone_number": clean_phone, 
            "country": None,
            "profile_url": None,
            "conversation_id": f"whatsapp_{clean_phone}",
            "email": None,
            "categories": [],
            "platform": "whatsapp",
            "created_at": now
        }

        bulk_ops.append(
            UpdateOne(
                {"phone_number": clean_phone}, 
                {"$setOnInsert": contact_doc}, 
                upsert=True
            )
        )

    if bulk_ops:
        result = contacts_collection.bulk_write(bulk_ops, ordered=False)
        return {
            "total_rows_in_file": len(df),
            "successfully_processed": result.upserted_count + result.matched_count,
            "newly_added": result.upserted_count,
            "skipped_invalid_rows": skipped_rows
        }
    
    raise ValueError("No valid contact data found in the file.")
