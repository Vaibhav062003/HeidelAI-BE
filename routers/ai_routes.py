from database import get_mongo_db
from schemas.models import ChatbotPromptUpdate
from auth.dependencies import get_current_user
from services.ai_chatbot import update_prompt_in_cache
from fastapi import APIRouter, HTTPException, Depends
from agents.followup_agent.follow_up_agent import get_followup
from core.services import services

db = get_mongo_db()

router = APIRouter(prefix="/api/ai", tags=["AI Routes"])

@router.get("/product-recommendations/{conversation_id}")
async def get_product_recommendations(conversation_id: str, user=Depends(get_current_user)):
    """
    This endpoint retrieves product recommendations for a specific conversation.
    """
    org_id = user.get("org_id")
    conversation_col_name = f"conversations_{org_id}"
    conversations_collection = db[conversation_col_name]
    
    conversation = conversations_collection.find_one({"id": conversation_id})

    if conversation:
        products = conversation.get("product_recommendations")
        if products:
            return {"status": "success", "data": products}
        return {"status": "error", "message": "No product recommendations found"}
        
    return {"status" : "Error", "message": "No conversation found"}


@router.get("/followup/{conversation_id}")
def get_followup_api(conversation_id: str, user=Depends(get_current_user)):
    """
    API endpoint to generate a follow-up message for a given conversation.

    Args:
        conversation_id (str): The unique ID of the conversation.
        user: The current user, used to retrieve organization ID.

    Returns:
        dict: JSON response containing the follow-up message.
    """
    try:
        followup_response = get_followup(user.get("org_id"), conversation_id)
        if followup_response:
            return followup_response
        else:
            raise HTTPException(status_code=500, detail="Failed to generate follow-up message.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.put("/chatbot-prompt")
def update_chatbot_prompt(payload: ChatbotPromptUpdate, user=Depends(get_current_user)):
    """
    Update the chatbot prompt for the authenticated user's organization.
    """
    org_id = user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user context.")

    result = db.organizations.update_one(
        {"org_id": org_id},
        {"$set": {"chatbot_prompt": payload.chatbot_prompt}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Organization not found.")

    update_prompt_in_cache(org_id, payload.chatbot_prompt)

    return {"status": "success", "message": "Chatbot prompt updated successfully."}
