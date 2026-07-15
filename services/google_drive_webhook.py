"""
google_drive_webhook.py — Google Drive push notification (watch channel) management.

Google's Drive API lets you "watch" a file for changes. When the file changes,
Google sends a POST to your registered webhook URL. This module handles the
full lifecycle: create, validate, renew, and stop watch channels.

Key concepts:
  - A watch channel has a max lifetime of ~24 hours (we use 23h for safety).
  - The webhook payload is metadata only (channel ID, resource state) — NOT row data.
  - Channels must be renewed before they expire; a background job handles this.
  - Each channel gets a random token stored in the DB for validation on receipt.
"""

import asyncio
import httpx
import uuid
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger
from database import get_mongo_db
from config.settings import GOOGLE_WEBHOOK_BASE_URL, GOOGLE_WEBHOOK_SECRET

db = get_mongo_db()

# Channel lifetime: 23 hours (Google max is ~24h, leave buffer for renewal)
WATCH_CHANNEL_LIFETIME_HOURS = 23

# Renew channels that expire within this window
RENEWAL_BUFFER_MINUTES = 60

# Webhook endpoint path (appended to GOOGLE_WEBHOOK_BASE_URL)
WEBHOOK_PATH = "/api/google-sheets/webhook"


# ─── Watch Token Generation ───────────────────────────────────────────────────

def _generate_watch_token(org_id: str, sheet_id: str, channel_id: str) -> str:
    """
    Generate a deterministic HMAC token for a watch channel.
    This token is sent by Google in the X-Goog-Channel-Token header on every
    notification, letting us verify the notification is genuine.
    """
    message = f"{org_id}:{sheet_id}:{channel_id}"
    return hmac.new(
        GOOGLE_WEBHOOK_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


# ─── Create Watch Channel ─────────────────────────────────────────────────────

async def create_watch_channel(
    sheet_id: str, org_id: str, access_token: str
) -> Optional[dict]:
    """
    Create a Drive API watch channel for a spreadsheet file.

    Calls POST https://www.googleapis.com/drive/v3/files/{fileId}/watch
    and stores the channel metadata in google_drive_watch_channels.

    Returns the channel document on success, None on failure.
    """
    if not GOOGLE_WEBHOOK_BASE_URL:
        logger.warning(
            "[Webhook] GOOGLE_WEBHOOK_BASE_URL not set — skipping watch channel creation. "
            "Falling back to reconciliation-only mode."
        )
        return None

    channel_id = str(uuid.uuid4())
    watch_token = _generate_watch_token(org_id, sheet_id, channel_id)
    webhook_url = GOOGLE_WEBHOOK_BASE_URL.rstrip("/") + WEBHOOK_PATH

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=WATCH_CHANNEL_LIFETIME_HOURS)
    expiration_ms = str(int(expires_at.timestamp() * 1000))

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://www.googleapis.com/drive/v3/files/{sheet_id}/watch",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "id": channel_id,
                    "type": "web_hook",
                    "address": webhook_url,
                    "token": watch_token,
                    "expiration": expiration_ms,
                },
                timeout=15,
            )

        if resp.status_code not in (200, 201):
            error_detail = resp.text[:500]
            logger.error(
                f"[Webhook] Failed to create watch channel for sheet {sheet_id} "
                f"org {org_id}: {resp.status_code} {error_detail}"
            )
            return None

        data = resp.json()
        resource_id = data.get("resourceId", "")
        actual_expiry_ms = data.get("expiration")

        # Use Google's actual expiration if provided (may differ from requested)
        if actual_expiry_ms:
            expires_at = datetime.fromtimestamp(
                int(actual_expiry_ms) / 1000, tz=timezone.utc
            )

        channel_doc = {
            "org_id": org_id,
            "sheet_id": sheet_id,
            "channel_id": channel_id,
            "resource_id": resource_id,
            "watch_token": watch_token,
            "expires_at": expires_at,
            "created_at": now,
        }

        await asyncio.to_thread(
            db.google_drive_watch_channels.insert_one, channel_doc
        )

        logger.info(
            f"[Webhook] Watch channel created: channel={channel_id} "
            f"sheet={sheet_id} org={org_id} expires={expires_at.isoformat()}"
        )
        return channel_doc

    except Exception as e:
        logger.error(
            f"[Webhook] Error creating watch channel for sheet {sheet_id} "
            f"org {org_id}: {e}"
        )
        return None


# ─── Stop Watch Channel ───────────────────────────────────────────────────────

async def stop_watch_channel(
    channel_id: str, resource_id: str, access_token: str
) -> bool:
    """
    Stop a Drive API watch channel.

    Calls POST https://www.googleapis.com/drive/v3/channels/stop.
    Returns True if the channel was stopped (or was already stopped).
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://www.googleapis.com/drive/v3/channels/stop",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "id": channel_id,
                    "resourceId": resource_id,
                },
                timeout=15,
            )

        # 204 = success, 404 = already stopped — both are fine
        if resp.status_code in (200, 204, 404):
            logger.info(f"[Webhook] Watch channel stopped: {channel_id}")
            return True

        logger.warning(
            f"[Webhook] Failed to stop watch channel {channel_id}: "
            f"{resp.status_code} {resp.text[:300]}"
        )
        return False

    except Exception as e:
        logger.error(f"[Webhook] Error stopping watch channel {channel_id}: {e}")
        return False


# ─── Stop All Channels for a Sheet ────────────────────────────────────────────

async def stop_all_channels_for_sheet(
    org_id: str, sheet_id: str, access_token: str
) -> int:
    """
    Stop and delete all active watch channels for a specific sheet.
    Called when a sheet is disconnected.
    Returns the number of channels stopped.
    """
    channels = await asyncio.to_thread(
        lambda: list(
            db.google_drive_watch_channels.find(
                {"org_id": org_id, "sheet_id": sheet_id},
                {"channel_id": 1, "resource_id": 1, "_id": 1},
            )
        )
    )

    stopped = 0
    for ch in channels:
        await stop_watch_channel(ch["channel_id"], ch["resource_id"], access_token)
        await asyncio.to_thread(
            db.google_drive_watch_channels.delete_one, {"_id": ch["_id"]}
        )
        stopped += 1

    if stopped:
        logger.info(
            f"[Webhook] Stopped {stopped} watch channel(s) for "
            f"sheet {sheet_id} org {org_id}"
        )
    return stopped


# ─── Validate Webhook Notification ────────────────────────────────────────────

async def validate_webhook_notification(
    channel_id: str, resource_id: str, token: str
) -> Optional[dict]:
    """
    Validate an incoming webhook notification against stored channels.

    Returns the channel document if valid, None otherwise.
    """
    if not channel_id or not resource_id:
        return None

    channel = await asyncio.to_thread(
        db.google_drive_watch_channels.find_one,
        {
            "channel_id": channel_id,
            "resource_id": resource_id,
            "watch_token": token,
        },
    )

    if not channel:
        logger.warning(
            f"[Webhook] Unknown or invalid notification: "
            f"channel={channel_id} resource={resource_id}"
        )
        return None

    # Check if channel has expired (Google might still send a straggler)
    if channel.get("expires_at") and channel["expires_at"] < datetime.now(timezone.utc):
        logger.warning(
            f"[Webhook] Received notification for expired channel {channel_id}, "
            f"expired at {channel['expires_at'].isoformat()}"
        )
        # Still process it — the data might be valid, and sync is idempotent
        return channel

    return channel


# ─── Renew Expiring Channels ─────────────────────────────────────────────────

async def renew_expiring_channels() -> int:
    """
    Find channels expiring within RENEWAL_BUFFER_MINUTES and replace them.

    For each expiring channel:
      1. Create a new watch channel (overlap period with old one is intentional)
      2. Stop the old channel
      3. Delete the old channel from DB

    Returns the number of channels renewed.
    """
    cutoff = datetime.now(timezone.utc) + timedelta(minutes=RENEWAL_BUFFER_MINUTES)

    expiring = await asyncio.to_thread(
        lambda: list(
            db.google_drive_watch_channels.find(
                {"expires_at": {"$lt": cutoff}},
                {
                    "org_id": 1, "sheet_id": 1, "channel_id": 1,
                    "resource_id": 1, "_id": 1,
                },
            )
        )
    )

    if not expiring:
        return 0

    logger.info(f"[Webhook] Renewing {len(expiring)} expiring watch channel(s)")

    renewed = 0
    for ch in expiring:
        org_id = ch["org_id"]
        sheet_id = ch["sheet_id"]
        old_channel_id = ch["channel_id"]
        old_resource_id = ch["resource_id"]

        try:
            # Get a fresh access token for this org
            from api.google_sheets_routes import refresh_google_token
            access_token = await refresh_google_token(org_id)

            # Create a new channel first (overlap period is safe — sync is idempotent)
            new_channel = await create_watch_channel(sheet_id, org_id, access_token)
            if not new_channel:
                logger.warning(
                    f"[Webhook] Failed to create replacement channel for "
                    f"sheet {sheet_id} org {org_id} — old channel will expire"
                )
                continue

            # Stop the old channel
            await stop_watch_channel(old_channel_id, old_resource_id, access_token)

            # Remove old channel from DB
            await asyncio.to_thread(
                db.google_drive_watch_channels.delete_one, {"_id": ch["_id"]}
            )

            renewed += 1
            logger.info(
                f"[Webhook] Renewed channel for sheet {sheet_id} org {org_id}: "
                f"{old_channel_id} → {new_channel['channel_id']}"
            )

        except Exception as e:
            logger.error(
                f"[Webhook] Failed to renew channel {old_channel_id} "
                f"for sheet {sheet_id} org {org_id}: {e}"
            )

    logger.info(f"[Webhook] Channel renewal complete: {renewed}/{len(expiring)} renewed")
    return renewed
