from datetime import datetime, timezone
from database import get_mongo_db
from fastapi import HTTPException
from pymongo.errors import PyMongoError

db = get_mongo_db()

async def reopen_conversation(org_id: str, conversation_id: str):
    """
    Reopen a closed conversation by removing the 'closed_at' field.
    """
    try:
        conversation_collection = db[f"conversations_{org_id}"]

        try:
            result = conversation_collection.update_one(
                {"conversation_id": conversation_id},
                {
                    "$unset": {
                        "closed_at": 1, 
                        "closure_reason": 1
                    },
                 "$set": {"started_at": datetime.now(timezone.utc)}}
            )
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error updating conversation: {str(e)}")

        if result.matched_count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"No conversation found with conversation_id={conversation_id}"
            )

        return {"message": "'closed_at' removed, conversation reopened successfully."}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
