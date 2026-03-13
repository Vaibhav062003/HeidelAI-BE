import requests
from database import get_mongo_db
from config.settings import ACCESS_TOKEN
from config.settings import APP_ID
from services.cloudinary_service import upload_media_to_cloudinary
import json
from fastapi import APIRouter, HTTPException, Response, Query, File, UploadFile, Form, status
from fastapi.responses import JSONResponse
from typing import Optional
import httpx

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
    update_contact_labels,
    add_tag_to_org_metadata,
    add_label_to_org_metadata,
    remove_tag_from_org_metadata,
    remove_label_from_org_metadata,
    get_tags_from_org_metadata,
    get_labels_from_org_metadata,
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

from services.wa_service import send_whatsapp_reaction
from loguru import logger


router = APIRouter(tags=["Common Routes"])
db = get_mongo_db()

@router.get("/")
async def health_check():
    return {"status": "ok"}

@router.head("/health", status_code=status.HTTP_200_OK)
async def health_check_head(response: Response):
    """
    Lightweight uptime check.
    Returns 200 OK with custom headers, but NO body content.
    """
    response.headers["X-App-Status"] = "Online"
    
    return None

@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204) 

@router.get("/api/conversations")
async def get_conversations(
    user: CurrentUser,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100), # Limit max fetch to 100
    search: Optional[str] = None,
    active_tab: str = Query("all"),
    unread_only: bool = Query(False),
    platform: Optional[str] = None,
    priority: Optional[str] = None
):
    """
    Get conversations for the current user's organization
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Invalid organization ID")

        return await fetch_conversations_new(
            org_id=org_id,
            current_user_id=user.user_id,
            skip=skip,
            limit=limit,
            search=search,
            active_tab=active_tab,
            unread_only=unread_only,
            platform=platform,
            priority=priority
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

@router.post("/api/contacts/update-labels")
async def update_contact_labels_endpoint(body: UpdateCategoriesRequest, user: CurrentUser):
    """
    Replace the labels array for a specific contact.
    """
    # Note: Using UpdateCategoriesRequest as a temporary solution since it has the same structure (conversation_id, categories/labels)
    # The frontend should send {conversation_id: "...", categories: ["label1", "label2"]} 
    # but we map 'categories' to 'labels' in the service call for consistency with how Pydantic handles the body.
    org_id = user.org_id

    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    
    return await update_contact_labels(org_id, body.conversation_id, body.categories)


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
    
@router.patch("/api/org/metadata/labels/add")
async def add_label(user: CurrentUser, label: str):
    """
    Add a label to the organization's metadata.
    """

    result = await add_label_to_org_metadata(user.org_id, label)

    if result == True:
        return {"message": "Label added successfully."}
    else:
        # label already exists
        raise HTTPException(status_code=400, detail="Label already exists.")
    
@router.patch("/api/org/metadata/tags/remove")
async def remove_tag(user: CurrentUser, tag: str):
    """
    Remove a tag from the organization's metadata.
    """

    result = await remove_tag_from_org_metadata(user.org_id, tag)

    return {"message": "Tag removed successfully."}

@router.patch("/api/org/metadata/labels/remove")
async def remove_label(user: CurrentUser, label: str):
    """
    Remove a label from the organization's metadata.
    """

    result = await remove_label_from_org_metadata(user.org_id, label)

    return {"message": "Label removed successfully."}

@router.get("/api/org/metadata/tags")
async def get_tags(user: CurrentUser):
    """Get tags for the organization."""

    result = await get_tags_from_org_metadata(user.org_id)

    return {"tags": result}

@router.get("/api/org/metadata/labels")
async def get_labels(user: CurrentUser):
    """Get labels for the organization."""

    result = await get_labels_from_org_metadata(user.org_id)

    return {"labels": result}


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

@router.post("/api/templates/upload-media")
async def upload_media(user: CurrentUser, file: UploadFile = File(...)):
    """
    Creates upload session and uploads file to Meta.
    Returns uploaded file handle.
    """
    org_id = user.org_id
    org_document = db.organizations.find_one({"org_id": org_id})

    wa_id = org_document.get("wa_id")
    logger.success(f"Fetched wa_id: {wa_id} for org_id: {org_id}")

    connection = db.whatsapp_connections.find_one(
            {"wa_id": wa_id, "org_id": org_id},
            {"_id": 0, "access_token": 1}

        )
    access_token = connection["access_token"] if connection else None

    if not connection or "access_token" not in connection:
        raise HTTPException(
            status_code=404,
            detail="WhatsApp connection not found or access token missing"
        )

    logger.success(f"Fetched WhatsApp connection for wa_id: {wa_id}")

    try:
        file_content = await file.read()
        file_length = len(file_content)

        async with httpx.AsyncClient() as client:

            # ---------------- STEP 1: Create Upload Session ----------------
            create_url = f"https://graph.facebook.com/v24.0/{APP_ID}/uploads"

            headers= {
                "Authorization": f"Bearer {access_token}"
            }

            data= {
                "messaging_product": "whatsapp",
            }

            
            params = {  
                "file_name": file.filename,
                "file_length": file_length,
                "file_type": file.content_type,
                "access_token": access_token
            }

            session_res = await client.post(create_url, params=params, headers=headers, data=data)

            if session_res.status_code != 200:
                raise HTTPException(
                    status_code=session_res.status_code,
                    detail=session_res.text
                )

            upload_session_id = session_res.json()["id"]  # upload:<ID>

            # ---------------- STEP 2: Upload File ----------------
            upload_url = f"https://graph.facebook.com/v24.0/{upload_session_id}"

            headers = {
                "Authorization": f"OAuth {access_token}",
                "file_offset": "0"
            }

            upload_res = await client.post(
                upload_url,
                headers=headers,
                content=file_content  # binary upload
            )

            if upload_res.status_code != 200:
                raise HTTPException(
                    status_code=upload_res.status_code,
                    detail=upload_res.text
                )

            return upload_res.json() 

    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Internal Server Error during upload."
        )

        
    
@router.post("/api/whatsapp/upload-media")
async def upload_whatsapp_media( user: CurrentUser, file: UploadFile = File(...)
):
    """
    Upload media to WhatsApp Cloud API.
    Returns media_id.
    """
    return await upload_media_to_cloudinary(
        file=file,
        platform="whatsapp",
        org_id=user.org_id
    )

@router.delete("/api/whatsapp/delete-template")
async def delete_whatsapp_template(user: CurrentUser, template_name: str):
    """
    Delete a WhatsApp template.
    """
    org_id = user.org_id
    org_document = db.organizations.find_one({"org_id": org_id})

    wa_id = org_document.get("wa_id")
    logger.success(f"Fetched wa_id: {wa_id} for org_id: {org_id}")

    connection = db.whatsapp_connections.find_one(
            {"wa_id": wa_id, "org_id": org_id},
            {"_id": 0, "access_token": 1}

        )
    access_token = connection["access_token"] if connection else None

    template_names = [name.strip() for name in template_name.split(",") if name.strip()]

    try:
        for name in template_names:
            url = f"https://graph.facebook.com/v23.0/{wa_id}/message_templates?name={name}"
            response = requests.delete(url, params={"access_token": access_token}) 

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=response.text
                )
        return {"message": "Templates deleted successfully"}
    except Exception as e:
        logger.error(f"Delete template failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Internal Server Error during deletion."
        )

@router.post("/api/organization/toggle_ai")
async def toggle_whatsapp_ai_status(user: CurrentUser, body: dict):
    """Toggle AI status for a specific organization"""
    try:
        org_id = user.org_id 
        ai_enabled = body.get("enabled")
        
        if ai_enabled is None:
            return {"status": "error", "message": "Incorrect request body"}
        
        # Update the organization
        result = db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"is_ai_enabled": ai_enabled}}
        )
        
        if result.matched_count == 0:
            return {"status": "error", "message": "Organization not found"}
        
        return {"status": "success", "message": f"AI status updated to {ai_enabled}"}
    
    except Exception as e:
        logger.error(f"Error updating WhatsApp AI status: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to update AI status: {str(e)}"
        )