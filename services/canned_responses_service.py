from services.utils import serialize_mongo
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from datetime import datetime, timezone
from pymongo import ReturnDocument
from fastapi import HTTPException, status
from bson.errors import InvalidId
from schemas.models import CannedResponse

from database import get_mongo_db

db = get_mongo_db()


async def get_canned_responses_for_org(org_id):
    """
    Retrieves canned responess of an organization 
    """

    collection = db["canned_responses"]

    canned_responses = list(collection.find(
        {"org_id": org_id}))

    canned_responses = [serialize_mongo(cr) for cr in canned_responses]

    return canned_responses

async def create_canned_response_for_org(org_id: str, data: CannedResponse):
    """
    Creates a canned response for an organization.
    Args:
        org_id (str): The organization ID.
        data (CannedResponse): 
            shortcut (str): The /shortcut keyword
            response: The text to replace the shortcut
    """
    shortcut = data.shortcut
    response = data.response
    now = datetime.now(timezone.utc)

    if not shortcut.startswith("/"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The shortcut should start with / - {shortcut}",
        )

    canned_responses_col = db.canned_responses

    document = {
        "org_id": org_id,
        "shortcut": shortcut,
        "response": response,
        "updated_at": now,
    }
    
    try:
        canned_responses_col.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Canned response with shortcut {shortcut} already exists",
        )

    canned_responses = list(
        canned_responses_col.find({"org_id": org_id})
    )

    return [serialize_mongo(cr) for cr in canned_responses]
    
async def delete_canned_response_for_org(org_id: str, shortcut_id: str):
    """
    Deletes a canned response for an organization based on the mongo_id.
    """

    try:
        object_id = ObjectId(shortcut_id)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{shortcut_id}' is not a valid canned response ID.",
        )

    canned_response = db.canned_responses.find_one_and_delete({
        "org_id": org_id,
        "_id": object_id
    })

    if not canned_response:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Canned response with ID '{shortcut_id}' not found.",
        )

    return serialize_mongo(canned_response)

async def update_canned_response_for_org(
    org_id: str,
    shortcut_id: str,
    new_response: str,
):
    """
    Updates the response for a canned response obj by _id
    and returns the updated document.
    """

    try:
        object_id = ObjectId(shortcut_id)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{shortcut_id}' is not a valid canned response ID.",
        )

    updated = db.canned_responses.find_one_and_update(
        {
            "org_id": org_id,
            "_id": object_id,
        },
        {
            "$set": {
                "response": new_response,
                "updated_at": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Canned response with ID '{shortcut_id}' not found.",
        )

    updated = serialize_mongo(updated)

    return updated