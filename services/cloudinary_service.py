# Built-in modules
import asyncio
import httpx
from typing import Optional
import requests
import io
import cloudinary
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

# Internal modules
from config.settings import (
    CLOUDINARY_CLOUD_NAME,
    CLOUDINARY_API_KEY,
    CLOUDINARY_API_SECRET,
)
from schemas.models import MessageRole
from core.logger import get_logger

logger = get_logger(__name__)

# Cloudinary configuration
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

# @app.get("/api/cloudinary/stats")
async def get_cloudinary_stats():
    """
    Get current Cloudinary storage usage statistics
    """
    try:
        cloud_name = CLOUDINARY_CLOUD_NAME
        api_key = CLOUDINARY_API_KEY
        api_secret = CLOUDINARY_API_SECRET
        
        # Request usage data from Cloudinary Admin API
        response = requests.get(
            f"https://api.cloudinary.com/v1_1/{cloud_name}/usage",
            auth=(api_key, api_secret)
        )
        
        if response.status_code != 200:
            return {
                "status": "error", 
                "message": "Failed to fetch Cloudinary stats",
                "error": response.text
            }
            
        usage_data = response.json()
        
        # Calculate storage in GB
        storage_bytes = usage_data.get("storage", {}).get("usage", 0)
        storage_gb = storage_bytes / (1024 * 1024 * 1024)  # Convert bytes to GB
        
        # Calculate percentage of 25GB limit
        percentage_used = (storage_gb / 25) * 100
        
        return {
            "status": "success",
            "storage": {
                "bytes": storage_bytes,
                "gigabytes": round(storage_gb, 2),
                "percentage_of_limit": round(percentage_used, 2)
            },
            "bandwidth": usage_data.get("bandwidth", {}),
            "resources": usage_data.get("resources", {}),
            "is_approaching_limit": percentage_used > 80
        }
        
    except Exception as e:
        logger.error(f"Error fetching Cloudinary stats: {e}")
        return {"status": "error", "message": str(e)}

async def cleanup_old_cloudinary_media(days_threshold=30):
    """
    Delete Cloudinary resources (image, video, raw) older than the specified threshold.
    Uses Search API to safely find files by date before deleting.
    """
    try:
        # 1. Calculate cutoff date in YYYY-MM-DD format (Required for Cloudinary Search)
        cutoff_date = datetime.now() - timedelta(days=days_threshold)
        cutoff_str = cutoff_date.strftime('%Y-%m-%d')
        
        folders = ["whatsapp_media", "instagram_media"]
        resource_types = ["image", "video", "raw"] # 'raw' covers PDFs, DOCs, etc.
        
        total_deleted = 0
        deleted_details = {}

        for folder in folders:
            for r_type in resource_types:
                # 2. Build Search Expression
                # Syntax: folder:name/* AND resource_type:type AND created_at<=date
                expression = f"folder:{folder}/* AND resource_type:{r_type} AND created_at<={cutoff_str}"
                
                # 3. Search for matching files (Limit 500 per run to be safe)
                # Note: If you have >500 old files, this runs daily so it will catch up eventually.
                search_result = cloudinary.Search()\
                    .expression(expression)\
                    .max_results(500)\
                    .execute()
                
                resources = search_result.get('resources', [])
                public_ids = [res['public_id'] for res in resources]
                
                if public_ids:
                    logger.info(f"Deleting {len(public_ids)} {r_type}s from {folder}...")
                    
                    # 4. Delete by Public IDs
                    delete_response = cloudinary.api.delete_resources(
                        public_ids, 
                        resource_type=r_type
                    )
                    
                    count = len(delete_response.get("deleted", {}))
                    total_deleted += count
                    deleted_details[f"{folder}_{r_type}"] = count
                else:
                    deleted_details[f"{folder}_{r_type}"] = 0

        return {
            "status": "success",
            "total_deleted": total_deleted,
            "details": deleted_details
        }
        
    except Exception as e:
        logger.error(f"Error cleaning up Cloudinary resources: {e}")
        return {"status": "error", "message": str(e)}

# @app.on_event("startup")
async def schedule_cleanup_tasks():
    """Schedule periodic tasks when the app starts"""
    # Create scheduler
    scheduler = BackgroundScheduler()
    
    # Schedule daily cleanup of media older than 30 days
    @scheduler.scheduled_job('cron', hour=3, minute=30)  # Run at 3:30 AM
    def cleanup_old_media():
        asyncio.create_task(cleanup_old_cloudinary_media(days_threshold=30))
    
    # Schedule weekly check of storage usage
    @scheduler.scheduled_job('cron', day_of_week='sun', hour=4)  # Run Sundays at 4 AM
    def check_storage_usage():
        asyncio.create_task(alert_if_storage_high())
        
    # Start the scheduler
    scheduler.start()

async def alert_if_storage_high():
    """Check storage usage and send alert if approaching limit"""
    stats = await get_cloudinary_stats()
    if stats.get("status") == "success" and stats["storage"]["percentage_of_limit"] > 75:
        # Send alert (email, Slack, etc.)
        logger.warning(f"Cloudinary storage at {stats['storage']['percentage_of_limit']}% of 25GB limit")
        # Implement notification system here

async def check_cloudinary_storage_limit():
    """Check if storage is approaching 25GB limit"""
    try:
        stats = await get_cloudinary_stats()
        if stats.get("status") == "success":
            if stats["storage"]["percentage_of_limit"] > 90:
                # Perform emergency cleanup if over 90%
                await cleanup_old_cloudinary_media(days_threshold=15)
            return stats["storage"]["percentage_of_limit"] < 95  # Allow uploads if under 95%
        return True  # Default to allowing uploads if check fails
    except Exception:
        return True  # Allow uploads if check fails
    
async def upload_and_send_whatsapp_media_background(
    media_url: str, 
    media_id: str, 
    org_id: str, 
    mime_type: str, 
    access_token: str, 
    content: str,
    message_type: str,
    whatsapp_business_id: str,
    customer_phone_no: str,
    sender_name2: str,
    message_id: str,
    conversation_id: str,
    media_name: str,
    caption: Optional[str] = None
) -> Optional[str]:
    """
    Downloads media from WhatsApp and uploads to Cloudinary.
    This is designed to be run as a BackgroundTask.
    """
    from services.wa_service import create_or_update_whatsapp_conversation, store_whatsapp_message
    logger.success(f"Background Task Started: Processing media {media_id}")
    
    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    if mime_type == "application/pdf":
        mime_type = "pdf"

    try:
        # 1. Download from WhatsApp (Async)
        async with httpx.AsyncClient() as client:
            response = await client.get(media_url, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"Error downloading from WhatsApp: {response.text}")
                return

        # 2. Prepare file in memory
        file_obj = io.BytesIO(response.content)
        file_obj.name = media_id # Helps Cloudinary identify the file

        cld_resource_type = "raw" 
    
        if "image" in mime_type:
            cld_resource_type = "image"
        elif "video" in mime_type:
            cld_resource_type = "video"
        elif "pdf" in mime_type:
            # Cloudinary allows PDFs as 'image' to enable page previews/thumbnails
            cld_resource_type = "image" 
            format_for_preview = "jpg"
        else:
            # For DOCX, XLSX, ZIP, etc.
            cld_resource_type = "raw"

        # 3. Upload to Cloudinary
        result = cloudinary.uploader.upload(
            file_obj,
            folder=f"whatsapp_media/{org_id}/customer/", 
            public_id=media_id,
            resource_type=cld_resource_type
        )

        secure_url = result.get("secure_url")
        file_format = result.get("format")
        file_size = result.get("bytes")
        file_pages = result.get("pages")

        logger.success(f"Upload Successful: {secure_url}")

        # 4. Generate Thumbnail URL
        # If we uploaded as image/video (including PDF), we can swap extension
        thumbnail_url = None
        
        if cld_resource_type == "image" and mime_type == "pdf":
            # Convert .../upload/v123/file.pdf -> .../upload/v123/file.jpg
            # Cloudinary usually returns the PDF link, so we force JPG for thumbnail
            if secure_url.endswith(".pdf"):
                thumbnail_url = secure_url[:-4] + ".jpg"

        # 5. Prepare Payload with Thumbnail
        payload = {
            "url": secure_url,
            "caption": caption,
            "thumbnail_url": thumbnail_url, 
            "file_name": media_name,
            "file_size": file_size,
            "file_format" : file_format,
            "file_pages": file_pages,
            "media_id": media_id,
            "mime_type": mime_type
        }

        logger.success(f"Uploaded: {secure_url}")
        logger.success(f"Preview:  {thumbnail_url}")

        # ---------------------------------------------------------
        # Here I am storing & broadcasting this type of message after upload is completed.
        # TODO: In future we want to notify frontend on new message
        # have this process in background, after upload send a new message
        # that image is ready with the Cloudinary URL.
        # Till then frontend will show loading

        # This is because -> 
        # Frontend immmediately receives message that a new image has arrived
        # but the image is not yet uploaded to Cloudinary, so it shows loading.
        # ---------------------------------------------------------

        # Create or update conversation
        await create_or_update_whatsapp_conversation(
            org_id=org_id,
            recipient_id=whatsapp_business_id,
            customer_phone_no=customer_phone_no,
            customer_name=sender_name2,
            last_message=content,
            last_sender="customer"
        )
        
        # Store in WhatsApp-specific collections
        await store_whatsapp_message(
            org_id=org_id,
            content=caption,
            conversation_id=conversation_id,
            customer_phone_no=customer_phone_no,
            type=message_type,
            payload=payload,
            sender_name=sender_name2,
            recipient_id=whatsapp_business_id,
            role=MessageRole.CUSTOMER,
            message_id=message_id,  # Use the ID from the webhook for deduplication
        )

    except Exception as e:
        logger.error(f"Background Upload Failed: {e}")

async def upload_media_to_cloudinary(file: any, platform: str, org_id: str) -> str:
    """
    Uploads a media file to Cloudinary under the specified platform folder.
    Returns the secure URL of the uploaded media.
    """
    storage_ok = await check_cloudinary_storage_limit()

    if not storage_ok:
        logger.error("Cloudinary storage limit approaching. Upload aborted.")
        return None

    try:
        if platform == "whatsapp":
            folder = f"whatsapp_media/{org_id}/user"
        elif platform == "instagram":
            folder = f"instagram_media/{org_id}/user"
        else:
            folder = f"other_media/{org_id}/user"
        # Determine resource type based on MIME type
        mime_type = file.content_type
        cld_resource_type = "raw" 

        if "image" in mime_type:
            cld_resource_type = "image"
        elif "video" in mime_type:
            cld_resource_type = "video"
        elif "pdf" in mime_type:
            cld_resource_type = "image"  # PDFs as images for previews
        else:
            cld_resource_type = "raw"

        # Upload to Cloudinary
        result = cloudinary.uploader.upload(
            file.file,
            folder=folder, 
            public_id=file.filename.split('.')[0],
            resource_type=cld_resource_type
        )

        secure_url = result.get("secure_url")
        logger.success(f"Media uploaded to Cloudinary: {secure_url}")
        return {"file_url": secure_url, "file_type": cld_resource_type}

    except Exception as e:
        logger.error(f"Error uploading media to Cloudinary: {e}")
        return None