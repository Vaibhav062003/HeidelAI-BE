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
from services.wa_service import create_or_update_whatsapp_conversation, store_whatsapp_message
from schemas.models import MessageRole

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
        print(f"Error fetching Cloudinary stats: {e}")
        return {"status": "error", "message": str(e)}

async def cleanup_old_cloudinary_media(days_threshold=30):
    """
    Delete Cloudinary resources older than the specified threshold
    """
    try:
        # Calculate cutoff date
        cutoff_date = datetime.now() - timedelta(days=days_threshold)
        cutoff_str = cutoff_date.strftime('%d-%m-%Y')
        
        # Delete old resources in the whatsapp_media folder
        wa_image_response = cloudinary.api.delete_resources_by_prefix(
            prefix="whatsapp_media",
            resource_type="image",
            type="upload",
            created_at={"lte": cutoff_str}
        )
        
        # Also clean up videos
        wa_video_response = cloudinary.api.delete_resources_by_prefix(
            prefix="whatsapp_media",
            resource_type="video",
            type="upload",
            created_at={"lte": cutoff_str}
        )

        # Delete old resources in the instagram_media folder
        ig_image_response = cloudinary.api.delete_resources_by_prefix(
            prefix="instagram_media",
            resource_type="image",
            type="upload",
            created_at={"lte": cutoff_str}
        )
        
        # Also clean up videos
        ig_video_response = cloudinary.api.delete_resources_by_prefix(
            prefix="instagram_media",
            resource_type="video",
            type="upload",
            created_at={"lte": cutoff_str}
        )
        
        return {
            "status": "success",
            "deleted_images": len(wa_image_response.get("deleted", {})) + len(ig_image_response.get("deleted", {})),
            "deleted_videos": len(wa_video_response.get("deleted", {})) + len(ig_video_response.get("deleted", {}))
        }
        
    except Exception as e:
        print(f"Error cleaning up Cloudinary resources: {e}")
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
        print(f"⚠️ WARNING: Cloudinary storage at {stats['storage']['percentage_of_limit']}% of 25GB limit")
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
    conversation_id: str
) -> Optional[str]:
    """
    Downloads media from WhatsApp and uploads to Cloudinary.
    This is designed to be run as a BackgroundTask.
    """
    print(f"Background Task Started: Processing media {media_id}")
    
    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    try:
        # 1. Download from WhatsApp (Async)
        async with httpx.AsyncClient() as client:
            response = await client.get(media_url, headers=headers)
            
            if response.status_code != 200:
                print(f"❌ Error downloading from WhatsApp: {response.text}")
                return

        # 2. Prepare file in memory
        file_obj = io.BytesIO(response.content)
        file_obj.name = media_id # Helps Cloudinary identify the file

        # 3. Upload to Cloudinary
        result = cloudinary.uploader.upload(
            file_obj,
            folder=f"whatsapp_media/{org_id}/customer/", 
            public_id=media_id,
            resource_type=mime_type.split('/')[0],  # 'image' or 'video'
        )

        secure_url = result.get("secure_url")
        print(f"✅ Upload Successful: {secure_url}")

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
        payload = {
            "url": secure_url,
            "media_id": media_id,
            "mime_type": mime_type
        }

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
            content=content,
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
        print(f"❌ Background Upload Failed: {e}")