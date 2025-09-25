from services.conversation_service import fetch_conversations, close_conversation
from auth.dependencies import CurrentUser
from fastapi import APIRouter, HTTPException, Response, Query

router = APIRouter(tags=["Common Routes"])


@router.get("/api/conversations")
async def get_instagram_conversations(user: CurrentUser, status: str = Query(default=None)):
    """
    Endpoint wrapper → delegates to service layer.
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Invalid organization ID")

        return fetch_conversations(org_id, status)

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching conversations for {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch conversations")
    
@router.post("/api/conversations/{conversation_id}/close")
async def close_conversation_endpoint(conversation_id: str, user: CurrentUser):
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    return close_conversation(org_id, conversation_id)

@router.get("/")
async def health_check():
    return {"status": "ok"}

@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204) 