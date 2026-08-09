# Built-in modules
import asyncio
import httpx
from typing import Optional
import requests
import io
import re
import uuid
import cloudinary
from datetime import datetime, timedelta

# Internal modules
from config.settings import (
    CLOUDINARY_CLOUD_NAME,
    CLOUDINARY_API_KEY,
    CLOUDINARY_API_SECRET,
    WA_VERSION,
)
from loguru import logger


# Cloudinary configuration
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

AUDIO_RETENTION_DAYS = 30
AUDIO_RETENTION_TAG = "heidelai-audio-note"


def _outbound_upload_options(semantic_type: str, convert_audio: bool, media_kind: Optional[str] = None) -> dict:
    options = {}
    if convert_audio:
        options["eager"] = [{"format": "ogg", "audio_codec": "opus"}] if media_kind == "voice_note" else [{"format": "mp3"}]
    if semantic_type == "audio" and media_kind == "voice_note":
        options["tags"] = [AUDIO_RETENTION_TAG]
    return options


def _audio_retention_expression(days_threshold: int) -> str:
    return f"tags={AUDIO_RETENTION_TAG} AND created_at<={days_threshold}d"


def _cleanup_expired_audio_notes_sync(days_threshold: int) -> int:
    resources_by_type = {}
    next_cursor = None

    while True:
        search = cloudinary.Search().expression(
            _audio_retention_expression(days_threshold)
        ).max_results(500)
        if next_cursor:
            search = search.next_cursor(next_cursor)
        result = search.execute()

        for resource in result.get("resources", []):
            public_id = resource.get("public_id")
            if public_id:
                resources_by_type.setdefault(resource.get("resource_type", "video"), []).append(public_id)

        next_cursor = result.get("next_cursor")
        if not next_cursor:
            break

    total_deleted = 0
    for resource_type, public_ids in resources_by_type.items():
        for start in range(0, len(public_ids), 100):
            batch = public_ids[start:start + 100]
            response = cloudinary.api.delete_resources(batch, resource_type=resource_type)
            total_deleted += sum(status == "deleted" for status in response.get("deleted", {}).values())
    return total_deleted


async def _download_whatsapp_media(
    client,
    media_url: Optional[str],
    media_id: str,
    mime_type: str,
    headers: dict,
    phone_number_id: Optional[str] = None,
) -> tuple[bytes, str]:
    if not media_url:
        media_response = await client.get(
            f"https://graph.facebook.com/{WA_VERSION or 'v24.0'}/{media_id}",
            headers=headers,
            params={"phone_number_id": phone_number_id} if phone_number_id else None,
        )
        if media_response.status_code != 200:
            raise RuntimeError("WhatsApp media URL could not be retrieved")
        media_metadata = media_response.json()
        media_url = media_metadata.get("url")
        mime_type = media_metadata.get("mime_type") or mime_type
        if not media_url:
            raise RuntimeError("WhatsApp media URL was missing")

    response = await client.get(media_url, headers=headers)
    if response.status_code != 200:
        raise RuntimeError("WhatsApp media could not be downloaded")
    return response.content, mime_type


async def cleanup_expired_audio_notes(days_threshold: int = AUDIO_RETENTION_DAYS) -> int:
    """Delete tagged Cloudinary audio assets after the retention period."""
    try:
        deleted = await asyncio.to_thread(_cleanup_expired_audio_notes_sync, days_threshold)
        if deleted:
            logger.info(f"Deleted {deleted} expired audio notes from Cloudinary")
        return deleted
    except Exception as exc:
        logger.error(f"Failed to delete expired audio notes: {exc}")
        return 0

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
                expression = f"folder:{folder}/* AND resource_type:{r_type} AND -tags={AUDIO_RETENTION_TAG} AND created_at<={cutoff_str}"
                
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
    media_url: Optional[str],
    media_id: str, 
    org_id: str, 
    mime_type: str, 
    access_token: str, 
    message_type: str,
    whatsapp_business_id: str,
    message_id: str,
    media_name: str,
    caption: Optional[str] = None,
    phone_number_id: Optional[str] = None,
) -> Optional[str]:
    """
    Downloads media from WhatsApp and uploads to Cloudinary.
    This is designed to be run as a BackgroundTask.
    """
    from services.wa_service import update_whatsapp_media_message
    logger.success(f"Background Task Started: Processing media {media_id}")
    
    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    if mime_type == "application/pdf":
        mime_type = "pdf"

    try:
        # Meta URLs are short-lived. Retry the complete retrieve/download/upload
        # operation, using the stable media ID each time.
        last_error = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    media_bytes, mime_type = await _download_whatsapp_media(
                        client, media_url, media_id, mime_type, headers, phone_number_id
                    )

                file_obj = io.BytesIO(media_bytes)
                file_obj.name = media_id
                cld_resource_type = "raw"
                if "image" in mime_type:
                    cld_resource_type = "image"
                elif "video" in mime_type or "audio" in mime_type:
                    cld_resource_type = "video"
                elif "pdf" in mime_type:
                    cld_resource_type = "image"

                result = await asyncio.to_thread(
                    cloudinary.uploader.upload,
                    file_obj,
                    folder=f"whatsapp_media/{org_id}/customer/",
                    public_id=media_id,
                    resource_type=cld_resource_type,
                    **({"tags": [AUDIO_RETENTION_TAG]} if message_type == "audio" else {}),
                )
                if not result.get("secure_url"):
                    raise RuntimeError("Cloudinary did not return a media URL")
                break
            except Exception as exc:
                last_error = exc
                media_url = None
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        else:
            raise RuntimeError("WhatsApp media processing failed after three attempts") from last_error

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
            "mime_type": mime_type,
            "media_status": "ready",
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

        # Update the durable placeholder and broadcast the same message ID.
        await update_whatsapp_media_message(
            org_id=org_id,
            message_id=message_id,
            payload=payload,
            recipient_id=whatsapp_business_id,
            status="received",
        )
        return secure_url

    except Exception as e:
        logger.error(f"Background Upload Failed: {e}")
        await update_whatsapp_media_message(
            org_id=org_id,
            message_id=message_id,
            payload={
                "media_id": media_id,
                "mime_type": mime_type,
                "file_name": media_name,
                "media_status": "failed",
            },
            recipient_id=whatsapp_business_id,
            status="received",
        )
        return None

def _outbound_media_spec(mime_type: str):
    base_mime = (mime_type or "").lower().split(";", 1)[0].strip()
    if base_mime.startswith("image/"):
        return "image", "image", False
    if base_mime.startswith("video/"):
        return "video", "video", False
    if base_mime.startswith("audio/"):
        supported = {"audio/aac", "audio/amr", "audio/mp4", "audio/mpeg", "audio/mp3", "audio/ogg"}
        is_supported = base_mime in supported and (base_mime != "audio/ogg" or "opus" in mime_type.lower())
        return "audio", "video", not is_supported
    return "document", "image" if base_mime == "application/pdf" else "raw", False


async def upload_media_to_cloudinary(file: any, platform: str, org_id: str, media_kind: Optional[str] = None) -> Optional[dict]:
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
        mime_type = (file.content_type or "").lower()
        semantic_type, cld_resource_type, convert_audio = _outbound_media_spec(mime_type)
        if media_kind == "voice_note" and not (mime_type.startswith("audio/ogg") and "opus" in mime_type):
            convert_audio = True
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", file.filename or "attachment")
        unique_name = f"{uuid.uuid4().hex}-{safe_name}"
        public_id = unique_name if cld_resource_type == "raw" else unique_name.rsplit(".", 1)[0]
        upload_options = _outbound_upload_options(semantic_type, convert_audio, media_kind)

        # Upload to Cloudinary
        result = await asyncio.to_thread(
            cloudinary.uploader.upload,
            file.file,
            folder=folder,
            public_id=public_id,
            resource_type=cld_resource_type,
            **upload_options,
        )

        secure_url = result.get("secure_url")
        if convert_audio:
            eager = result.get("eager") or []
            if not eager or not eager[0].get("secure_url"):
                raise ValueError("Cloudinary did not return the converted audio file")
            secure_url = eager[0]["secure_url"]
            voice_note = media_kind == "voice_note"
            mime_type = "audio/ogg; codecs=opus" if voice_note else "audio/mpeg"
            extension = "ogg" if voice_note else "mp3"
            safe_name = f"{safe_name.rsplit('.', 1)[0]}.{extension}"
        logger.success(f"Media uploaded to Cloudinary: {secure_url}")
        return {
            "file_url": secure_url,
            "file_type": semantic_type,
            "mime_type": mime_type,
            "file_name": safe_name,
        }

    except Exception as e:
        logger.error(f"Error uploading media to Cloudinary: {e}")
        return None
