import json
from fastapi import APIRouter, HTTPException, Response, Query, File, UploadFile, Form
from fastapi.responses import JSONResponse
from typing import Optional

from auth.dependencies import CurrentUser
from core.services import services
from schemas.models import UnarchiveRequest, PrivateNoteRequest, UpdateCategoriesRequest, CannedResponse, SendReactionRequest
from services.conversation_utils import reopen_conversation
from services.common_service import (
    fetch_conversations_new, 
    fetch_conversation,
    send_message,
    fetch_messages,
    close_conversation, 
    unarchive_data, 
    add_private_note, 
    delete_private_note)
from services.contacts_service import (
    get_contacts_for_org, 
    update_contact_tag_categories,
    add_tag_to_org_metadata,
    remove_tag_from_org_metadata,
    get_tags_from_org_metadata,
    process_bulk_wa_contact_upload
    )
from services.ig_service import send_instagram_reaction
from services.products_service import (
    get_products_for_org, 
    bulk_upload_products_from_file, 
    add_single_product,
    get_product_by_id,
    delete_product_by_id
)

from core.logger import get_logger
from services.wa_service import send_whatsapp_reaction

logger = get_logger(__name__)

router = APIRouter(tags=["Common Routes"])

@router.get("/")
async def health_check():
    return {"status": "ok"}

@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204) 

@router.get("/api/conversations")
async def get_conversations(
    user: CurrentUser
):
    """
    Get conversations for the current user's organization, 
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Invalid organization ID")

        return await fetch_conversations_new(
            org_id=org_id
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching conversations for {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch conversations")

@router.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user: CurrentUser):
    """
    Fetch the conversation of a specific user for the organization.
    Returns the conversation in the standard format for compatibility with the frontend
    """
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    
    try:
        conversation = await fetch_conversation(org_id, conversation_id)
        return conversation
    except HTTPException as e:
        logger.error(f"Error fetching conversation {conversation_id} for org {org_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch conversation")

@router.get("/api/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: str, 
                       user: CurrentUser,
                       agent_username: str = Query(..., description="Username of the agent accessing this conversation")
                       ):
    """Fetch messages for a specific Instagram conversation"""
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    
    return await fetch_messages(org_id, conversation_id, user.user_id, agent_username)

@router.post("/api/conversations/send-message")
async def send_message_endpoint(
    user: CurrentUser,
    conversation_id: str = Form(...),
    content: Optional[str] = Form(None),
    platform: str = Form(...),
    mode: str = Form(...),
    temp_id: Optional[str] = Form(None), 
    file: Optional[UploadFile] = File(None),
    context_type: Optional[str] = Form(None),
    context: Optional[str] = Form(None)
):    
    """
    Send a message to a specific conversation.
    The body should contain the necessary fields for sending the message.
    """
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    context_dict = None
    if context:
        try:
            context_dict = json.loads(context)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format in context field")

    res = await send_message(
        org_id, 
        platform, 
        conversation_id, 
        content if content else None, 
        mode,
        file if file else None, 
        temp_id if temp_id else None,
        context_type if context_type else None,
        context_dict
    )

    return {"message": "Message sent", "details": res}

@router.post("/api/conversations/send_reaction")
async def send_reaction_endpoint(
    user: CurrentUser,
    data: SendReactionRequest
):
    """
    Send a reaction to a specific message in a conversation.
    """
    message_id = data.message_id
    emoji = data.emoji
    platform = data.platform
    org_id = user.org_id

    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    if platform == "instagram":
        res = await send_instagram_reaction(
            org_id,
            message_id,
            emoji
        )
    elif platform == "whatsapp":
        res = await send_whatsapp_reaction(
            org_id,
            message_id,
            emoji
        )
    response_status = res.get("status")
    response_message = res.get("message", "")
    if response_status == "error":
        raise HTTPException(status_code=500, detail="Failed to send reaction : " + response_message)

    return {"message": "Reaction sent", "details": res}

@router.post("/api/conversations/{conversation_id}/reopen")
async def reopen_conversation_endpoint(user: CurrentUser, conversation_id: str):
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    return await reopen_conversation(org_id, conversation_id)

@router.post("/api/conversations/{conversation_id}/close")
async def close_conversation_endpoint(user: CurrentUser, conversation_id: str):
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    return await close_conversation(org_id, conversation_id, "Closed by agent")

@router.post("/api/unarchive-data/{platform}")
async def unarchive(user: CurrentUser, platform: str, request: UnarchiveRequest):
    """
    Unarchive data for a specific platform.
    """
    logger.info(f"Unarchiving data for user: {user}")
    org_id = user.org_id
    user_id = request.platform_id

    result = await unarchive_data(org_id, platform, user_id)

    return result

@router.post("/api/conversations/{conversation_id}/private-note")
async def post_private_note(user: CurrentUser, conversation_id: str, body: PrivateNoteRequest):
    """
    This endpoint adds a new private note for a specific conversation.
    Private notes are stored in the private_notes collection.
    Each note is immutable once created.
    """
    org_id = user.org_id
    role = user.role

    note = body.note
    posted_by = body.posted_by
    platform = body.platform
    connection_id = body.connection_id

    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    
    try:
        res = await add_private_note(org_id, conversation_id, note, posted_by, role, platform, connection_id)

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    if not res:
        raise HTTPException(status_code=404, detail="Failed to add private note, no match found for conversation ID")

    return {"message": "Private note added successfully."}

# @router.delete("/api/conversations/{conversation_id}/private-note")
# async def remove_private_note(user: CurrentUser, note_id: str):
#     """
#     This endpoint deletes a private note for a specific conversation.
#     """
#     org_id = user.org_id

#     if not org_id:
#         raise HTTPException(status_code=400, detail="Invalid organization ID")
    
#     try:
#         await delete_private_note(org_id, note_id)
#     except HTTPException as e:
#         raise e
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to delete private note: {str(e)}")
    
#     return {"message": "Private note deleted successfully."}

@router.get("/api/contacts")
async def get_contacts(user: CurrentUser):
    """
    Retrieve contacts
    """
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    contacts = await get_contacts_for_org(org_id)

    return {"contacts": contacts}

@router.post("/api/contacts/update-categories")
async def update_contact_categories(body: UpdateCategoriesRequest, user: CurrentUser):
    """
    Replace the categories array for a specific contact.
    """
    org_id = user.org_id

    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    
    return await update_contact_tag_categories(org_id, body.conversation_id, body.categories)

@router.get("/api/products")
def list_products(user: CurrentUser):
    """
    """
    org_id = user.org_id

    products = get_products_for_org(org_id)
    
    return {"products": products}

@router.post("/api/products/bulk-upload")
async def bulk_upload(
    user: CurrentUser,
    file: UploadFile = File(...)
):
    """
    Bulk upload endpoint.
    - file: xlsx or csv with columns product_name (required), description, price, stock, product_image
    - images_zip (optional): zip file where filenames match product_image values in spreadsheet
    """
    org_id = user.org_id

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")

    result = await bulk_upload_products_from_file(org_id, file, None)
    return JSONResponse(content=result)

@router.post("/api/products")
async def create_product(
    user: CurrentUser,
    product_name: str = Form(...),
    description: str = Form(None),
    price: float = Form(...),
    stock: int = Form(None),
    image_url: str = Form(None),                 
):
    org_id = user.org_id

    data = {
        "product_name": product_name,
        "description": description,
        "price": price,
        "stock": stock,
        "image_url": image_url            
    }

    product = await add_single_product(org_id, data)
    return {"product": product}

@router.get("/api/products/{product_mongo_id}")
def get_product(user: CurrentUser, product_mongo_id: str):
    product = get_product_by_id(user.org_id, product_mongo_id)
    return {"product": product}

@router.delete("/api/products/{product_mongo_id}")
def delete_product(user: CurrentUser, product_mongo_id: str):
    deleted = delete_product_by_id(user.org_id, product_mongo_id)
    return {"deleted": deleted}

@router.patch("/api/org/metadata/tags/add")
async def add_tag(user: CurrentUser, tag: str):
    """
    Add a tag to the organization's metadata.
    """

    result = await add_tag_to_org_metadata(user.org_id, tag)

    if result == True:
        return {"message": "Tag added successfully."}
    else:
        # tag already exists
        raise HTTPException(status_code=400, detail="Tag already exists.")
    
@router.patch("/api/org/metadata/tags/remove")
async def remove_tag(user: CurrentUser, tag: str):
    """
    Remove a tag from the organization's metadata.
    """

    result = await remove_tag_from_org_metadata(user.org_id, tag)

    return {"message": "Tag removed successfully."}

@router.get("/api/org/metadata/tags")
async def get_tags(user: CurrentUser):
    """Get tags for the organization."""

    result = await get_tags_from_org_metadata(user.org_id)

    return {"tags": result}

@router.get("/api/agents-on-conversation")
async def get_agents_on_conversation():
    """
    For get_agents_on_conversation
    Returns the presence maps with string keys for JSON compatibility.
    """
    json_convo_viewers = {
        f"{org_id}|{convo_id}": list(client_ids)
        for (org_id, convo_id), client_ids in services.convo_viewers.items()
    }
    
    json_client_convo_map = {
        client_id: f"{org_id}|{convo_id}"
        for client_id, (org_id, convo_id) in services.client_convo_map.items()
    }
    
    return { 
        "convo_viewers": json_convo_viewers, 
        "client_convo_map": json_client_convo_map 
    }

@router.post("/api/contacts/bulk-upload")
async def bulk_upload_whatsapp_contacts(user: CurrentUser, file: UploadFile = File(...)):
    """
    Endpoint to upload CSV or Excel file for bulk whatsapp contact storage.
    """
    try:
        org_id = user.org_id

        content = await file.read()
        
        stats = process_bulk_wa_contact_upload(org_id, content, file.filename)
        
        return {
            "message": f"Successfully processed {file.filename}",
            "data": stats
        }

    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Bulk upload failed: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error during processing.")