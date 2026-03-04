from datetime import datetime, timezone
from unittest import result
from database import get_mongo_db
from fastapi import HTTPException
from pymongo.errors import PyMongoError
from pymongo import ReturnDocument
from services.utils import serialize_mongo
from core.logger import get_logger
# from services.websocket_service import broadcast_on_stats_update

logger = get_logger(__name__)

db = get_mongo_db()

async def reopen_conversation(org_id: str, conversation_id: str):
    """
    Reopen a closed conversation by removing the 'closed_at' field.
    """
    try:
        conversation_collection = db[f"conversations_{org_id}"]

        try:
            result = conversation_collection.find_one_and_update(
                {"conversation_id": conversation_id},
                {
                    "$unset": {
                        "closed_at": 1, 
                        "closure_reason": 1
                    },
                 "$set": {
                    "started_at": datetime.now(timezone.utc),
                    "last_message_timestamp": datetime.now(timezone.utc),
                    "status": "open"
                    }
                },
                return_document=ReturnDocument.AFTER
            )
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error updating conversation: {str(e)}")

        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"No conversation found with conversation_id={conversation_id}"
            )
        
        result = serialize_mongo(result)

        # Broadcast stats update
        # try:
        #     await broadcast_on_stats_update(org_id)
        # except Exception as e:
        #     logger.error(f"Failed to broadcast stats update for org {org_id} after reopen: {e}")

        return {"conversation": result}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
