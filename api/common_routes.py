from fastapi import APIRouter, HTTPException, Response, Query

from typing import Optional
from auth.dependencies import CurrentUser
from schemas.models import UnarchiveRequest, PrivateNoteRequest
from services.common_service import fetch_conversations, close_conversation, unarchive_data, create_or_update_private_note, delete_private_note

router = APIRouter(tags=["Common Routes"])

@router.get("/")
async def health_check():
    return {"status": "ok"}

@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204) 

@router.get("/api/conversations")
async def get_conversations(
    user: CurrentUser, 
    status: Optional[str] = Query(default=None),
    platform: Optional[str] = Query(default=None),
    priority: Optional[str] = Query(default=None),
    # unread_only: Optional[bool] = Query(default=False)
):
    """
    Get conversations for the current user's organization, 
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Invalid organization ID")

        return await fetch_conversations(
            org_id=org_id,
            status=status,
            platform=platform,
            priority=priority,
            # unread_only=unread_only,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching conversations for {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch conversations")
    
@router.post("/api/conversations/{conversation_id}/close")
async def close_conversation_endpoint(user: CurrentUser, conversation_id: str):
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    return await close_conversation(org_id, conversation_id)

@router.post("/api/unarchive-data/{platform}")
async def unarchive(user: CurrentUser, platform: str, request: UnarchiveRequest):
    """
    Unarchive data for a specific platform.
    """
    print(type(user))
    org_id = user.org_id
    user_id = request.platform_id

    result = await unarchive_data(org_id, platform, user_id)

    return result

@router.put("/api/conversations/{conversation_id}/private-note")
async def update_private_note(user: CurrentUser, conversation_id: str, body: PrivateNoteRequest):
    """
    This endpoint creates or updates a private note for a specific conversation.
    """
    org_id = user.org_id
    note = body.note
    updated_by = body.updated_by

    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    
    try:
        res = await create_or_update_private_note(org_id, conversation_id, note, updated_by)

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    if not res:
        raise HTTPException(status_code=404, detail="Failed to update private note, no match found for conversation ID")
    
    return {"message": "Private note updated successfully."}

@router.delete("/api/conversations/{conversation_id}/private-note")
async def remove_private_note(user: CurrentUser, conversation_id: str):
    """
    This endpoint deletes a private note for a specific conversation.
    """
    org_id = user.org_id
    print(user)

    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    
    try:
        await delete_private_note(org_id, conversation_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete private note: {str(e)}")
    
    return {"message": "Private note deleted successfully."}