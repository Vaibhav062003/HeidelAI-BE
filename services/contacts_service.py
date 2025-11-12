from datetime import datetime, timezone
from fastapi import BackgroundTasks, HTTPException
from pymongo.errors import PyMongoError

# Internal Modules
from core.services import services
from database import get_mongo_db

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
        print(f"Unknown platform {platform} - skipping contact storage.")
        return

    if contacts_collection.find_one(unique_filter):
        print(f"[Contacts] Contact already exists for {platform}: {unique_filter}")
        return
    
    contacts_collection.insert_one(contact_data)
    print(f"[Contacts] Added new contact for {platform}: {unique_filter}")

async def get_contacts_for_org(org_id: str):
    """
    Retrieve all contacts for a given organization.
    """
    try:
        collection_name = f"contacts_{org_id}"

        if collection_name not in db.list_collection_names():
            print(f"[INFO] Collection {collection_name} does not exist.")
            return []

        contacts_collection = db[collection_name]

        projection = {"_id": 0}

        cursor = contacts_collection.find({}, projection).batch_size(services.BATCH_SIZE)

        contacts = []
        for contact in cursor:
            contacts.append(contact)

        print(f"[INFO] Retrieved {len(contacts)} contacts for org {org_id}.")
        return contacts

    except PyMongoError as e:
        print(f"[ERROR] MongoDB error while fetching contacts for {org_id}: {e}")
        return []

    except Exception as e:
        print(f"[ERROR] Unexpected error in get_contacts_for_org({org_id}): {e}")
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
        print(f"[ERROR] MongoDB error while updating categories for {org_id}: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred")

    except Exception as e:
        print(f"[ERROR] Unexpected error in update_contact_categories: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error occurred")