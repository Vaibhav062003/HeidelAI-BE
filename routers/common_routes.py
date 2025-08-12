from datetime import datetime
from database import get_mongo_db
from auth.dependencies import get_current_user
from fastapi import APIRouter, Depends, HTTPException

db = get_mongo_db()

router = APIRouter(prefix="/api", tags=["Common Routes"])

@router.get("/conversations")
async def get_instagram_conversations(user=Depends(get_current_user)):
    """
    Fetch all the Instagram conversations for a specific Organization using org_id
    Returns them in the standard conversation format for compatibility with the frontend
    """
    try:
        org_id = user.get("org_id")
        # organization specific collection name for conversations
        org_conversations_collection_name = f"conversations_{org_id}"
        
        # Check if collection exists in database
        if org_conversations_collection_name not in db.list_collection_names():
            return []
        
        # Get the collection
        org_conversations_collection = db[org_conversations_collection_name]

        # Fetch conversations and sort by last_message_timestamp
        raw_conversations = list(org_conversations_collection.find(
            {}, 
            {"_id": 0}  # Exclude _id field
        ).sort("last_message_timestamp", -1))
        
        # Format conversations to match the required structure
        formatted_conversations = []
        for conv in raw_conversations:
            # Format timestamps
            timestamp = conv.get("last_message_timestamp") or conv.get("timestamp")
            if timestamp and isinstance(timestamp, datetime):
                timestamp = timestamp.isoformat()
            
            # Format the conversation object to match expected structure
            formatted_conversation = {
                'id': conv.get('conversation_id', conv.get('id')),
                'platform': conv.get('platform'),
                'customer_id': conv.get('customer_username'),
                'customer_name': f"{conv.get('customer_name', '')}",
                'last_message': conv.get('last_message', ''),
                'timestamp': timestamp,
                'unread_count': conv.get('unread_count', 0),
                'is_ai_enabled': conv.get('is_ai_enabled', True),
                'assigned_agent_id': conv.get('assigned_agent_id'),
                'priority': conv.get('priority', 'medium'),  # Default to medium
                'sentiment': conv.get('sentiment', 'neutral'),  # Default to neutral
                'query': conv.get('query', '')  # Optional query field
            }
            
            formatted_conversations.append(formatted_conversation)

        return formatted_conversations
    
    except Exception as e:
        print(f"Error fetching conversations for {org_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch conversations: {str(e)}"
        )