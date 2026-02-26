import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_mongo_db
from auth.dependencies import CurrentUser
from config.settings import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
from core.logger import get_logger

logger = get_logger(__name__)
db = get_mongo_db()

router = APIRouter(prefix="/api/google-sheets", tags=["Google Sheets"])


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class ManualSyncRequest(BaseModel):
    sheet_id: str


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/connections")
async def list_google_sheets_connections(user: CurrentUser):
    """
    Return all sheets connected to this org.
    Used by GoogleLoginDialog to display the connected sheets list.

    Response shape (matches ConnectedSheet interface in frontend):
    {
        "connections": [
            {
                "sheet_id": str,
                "sheet_name": str,
                "last_synced_at": str | null,
                "last_sync_rows": int,
                "polling_enabled": bool,
                "last_processed_row_number": int
            }
        ]
    }
    """
    try:
        org_id = user.org_id

        # Verify active Google connection exists
        google_conn = db.google_connections.find_one(
            {"org_id": org_id, "status": "active"},
            {"_id": 0, "credentials": 0}
        )

        if not google_conn:
            raise HTTPException(
                status_code=404,
                detail="No active Google connection found. Please connect your Google account first."
            )

        sheets = list(db.google_sheets_connections.find(
            {"org_id": org_id},
            {
                "_id": 0,
                "sheet_id": 1,
                "sheet_name": 1,
                "last_synced_at": 1,
                "last_sync_rows": 1,
                "polling_enabled": 1,
                "last_processed_row_number": 1,
            }
        ).sort("connected_at", -1))

        # Normalize datetime fields
        for sheet in sheets:
            if sheet.get("last_synced_at") and isinstance(sheet["last_synced_at"], datetime):
                sheet["last_synced_at"] = sheet["last_synced_at"].isoformat()

        return {"connections": sheets}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing Google Sheets connections for org {user.org_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list connected sheets: {str(e)}")


@router.post("/manual-sync")
async def manual_sync_sheet(request: ManualSyncRequest, user: CurrentUser):
    """
    Trigger an immediate on-demand sync for a single sheet.

    Response:
      { "status": "no_new_data" }                    - sheet already up to date
      { "status": "synced", "rows_processed": int }  - new rows found and processed
    """
    try:
        org_id = user.org_id
        sheet_id = request.sheet_id

        if not sheet_id:
            raise HTTPException(status_code=400, detail="sheet_id is required")

        # Verify sheet belongs to this org
        sheet_doc = db.google_sheets_connections.find_one(
            {"org_id": org_id, "sheet_id": sheet_id}
        )

        if not sheet_doc:
            raise HTTPException(status_code=404, detail="Sheet not found or not connected to this organization")

        # Perform the sync
        result = await _sync_sheet(org_id, sheet_id)

        rows_processed = result.get("rows_processed", 0)

        if rows_processed == 0:
            return {"status": "no_new_data", "rows_processed": 0}

        return {"status": "synced", "rows_processed": rows_processed}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual sync error for sheet {request.sheet_id} org {user.org_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.patch("/connections/{sheet_id}/toggle-polling")
async def toggle_sheet_polling(sheet_id: str, user: CurrentUser):
    """
    Toggle polling_enabled for a sheet (enable / pause auto-sync).

    Response:
      { "sheet_id": str, "polling_enabled": bool }
    """
    try:
        org_id = user.org_id

        sheet_doc = db.google_sheets_connections.find_one(
            {"org_id": org_id, "sheet_id": sheet_id}
        )

        if not sheet_doc:
            raise HTTPException(status_code=404, detail="Sheet not found")

        new_state = not sheet_doc.get("polling_enabled", True)
        now = datetime.now(timezone.utc)

        db.google_sheets_connections.update_one(
            {"org_id": org_id, "sheet_id": sheet_id},
            {"$set": {"polling_enabled": new_state, "updated_at": now.isoformat()}}
        )

        logger.info(
            f"Sheet {sheet_id} polling {'enabled' if new_state else 'disabled'} for org {org_id}"
        )

        return {"sheet_id": sheet_id, "polling_enabled": new_state}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling polling for sheet {sheet_id} org {user.org_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to toggle polling: {str(e)}")


# ─── Internal Helpers (used by scheduler + manual sync) ───────────────────────

async def refresh_google_token(org_id: str) -> Optional[str]:
    """
    Refresh the Google access token for an org if it's expired or about to expire.
    Updates the stored credentials in MongoDB.

    Returns:
        Valid access_token str, or raises HTTPException if refresh fails.
    """
    connection = db.google_connections.find_one({"org_id": org_id, "status": "active"})

    if not connection:
        raise HTTPException(status_code=404, detail=f"No active Google connection for org {org_id}")

    credentials = connection.get("credentials", {})
    access_token = credentials.get("access_token")
    refresh_token = credentials.get("refresh_token")
    token_expiry_str = credentials.get("token_expiry")

    # Check if token is still valid (with 5-minute buffer)
    if token_expiry_str and access_token:
        try:
            token_expiry = datetime.fromisoformat(token_expiry_str)
            if token_expiry.tzinfo is None:
                token_expiry = token_expiry.replace(tzinfo=timezone.utc)
            buffer = timedelta(minutes=5)
            if datetime.now(timezone.utc) + buffer < token_expiry:
                return access_token  # Token still valid
        except Exception:
            pass  # Fall through to refresh

    if not refresh_token:
        raise HTTPException(
            status_code=401,
            detail=f"Google refresh token not found for org {org_id}. User must reconnect."
        )

    # Refresh the token
    logger.info(f"Refreshing Google access token for org {org_id}")

    try:
        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10
        )

        if token_response.status_code != 200:
            error_info = token_response.json()
            error_desc = error_info.get("error_description", "Token refresh failed")
            logger.error(f"Google token refresh failed for org {org_id}: {error_desc}")

            # If refresh token is invalid/revoked, mark connection as inactive
            if error_info.get("error") in ("invalid_grant", "token_expired"):
                db.google_connections.update_one(
                    {"org_id": org_id},
                    {"$set": {"status": "inactive", "updated_at": datetime.now(timezone.utc).isoformat()}}
                )

            raise HTTPException(status_code=401, detail=f"Failed to refresh Google token: {error_desc}")

        token_info = token_response.json()
        new_access_token = token_info.get("access_token")
        expires_in = token_info.get("expires_in", 3600)

        if not new_access_token:
            raise HTTPException(status_code=401, detail="Google token refresh returned no access token")

        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Update stored credentials
        db.google_connections.update_one(
            {"org_id": org_id},
            {
                "$set": {
                    "credentials.access_token": new_access_token,
                    "credentials.token_expiry": new_expiry.isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            }
        )

        logger.info(f"Google access token refreshed for org {org_id}, expires {new_expiry.isoformat()}")
        return new_access_token

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error for org {org_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Token refresh error: {str(e)}")


async def _sync_sheet(org_id: str, sheet_id: str) -> dict:
    """
    Core sheet sync logic.
    Fetches rows after last_processed_row_number, stores them,
    updates last_synced_at and last_processed_row_number.

    Returns:
        { "rows_processed": int }
    """
    sheet_doc = db.google_sheets_connections.find_one(
        {"org_id": org_id, "sheet_id": sheet_id}
    )

    if not sheet_doc:
        raise HTTPException(status_code=404, detail=f"Sheet {sheet_id} not found for org {org_id}")

    last_row = sheet_doc.get("last_processed_row_number", 1)  # Row 1 = header, start at row 2
    sheet_name = sheet_doc.get("sheet_name", "Sheet1")
    tab_name = sheet_doc.get("tab_name", "Sheet1")  # spreadsheet tab name

    # Get a valid access token (auto-refreshes if needed)
    access_token = await refresh_google_token(org_id)

    # ── Fetch rows from Google Sheets API ─────────────────────────────────────
    # Range: from the row AFTER last processed, up to a reasonable limit
    start_row = last_row + 1  # +1 because we already processed up to last_row
    end_row = start_row + 999  # fetch up to 1000 rows per sync

    range_notation = f"{tab_name}!A{start_row}:ZZ{end_row}"
    sheets_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_notation}"

    response = requests.get(
        sheets_url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15
    )

    if response.status_code == 401:
        # Token was invalid despite refresh — force refresh and retry once
        logger.warning(f"401 from Sheets API for sheet {sheet_id}, forcing token refresh")
        access_token = await _force_token_refresh(org_id)
        response = requests.get(
            sheets_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15
        )

    if response.status_code == 404:
        logger.error(f"Sheet {sheet_id} not found in Google Sheets API")
        raise HTTPException(status_code=404, detail="Spreadsheet not found. It may have been deleted or access revoked.")

    if response.status_code != 200:
        error_msg = response.json().get("error", {}).get("message", "Unknown error")
        logger.error(f"Google Sheets API error for sheet {sheet_id}: {error_msg}")
        raise HTTPException(status_code=502, detail=f"Google Sheets API error: {error_msg}")

    data = response.json()
    values = data.get("values", [])

    if not values:
        # No new rows
        return {"rows_processed": 0}

    # ── Also get headers (row 1) to build structured dicts ────────────────────
    headers_range = f"{tab_name}!A1:ZZ1"
    headers_response = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{headers_range}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10
    )

    headers = []
    if headers_response.status_code == 200:
        header_values = headers_response.json().get("values", [[]])
        headers = header_values[0] if header_values else []

    # ── Build row dicts and store in MongoDB ──────────────────────────────────
    rows_processed = 0
    now = datetime.now(timezone.utc)
    new_last_row = last_row

    sheet_rows_collection = db[f"google_sheet_rows_{org_id}"]

    for idx, row_values in enumerate(values):
        actual_row_number = start_row + idx

        # Build a dict mapping header -> value (or col_N for unnamed columns)
        row_data = {}
        for col_idx, val in enumerate(row_values):
            col_name = headers[col_idx] if col_idx < len(headers) else f"col_{col_idx + 1}"
            row_data[col_name] = val

        # Deduplicate by (sheet_id, row_number)
        existing = sheet_rows_collection.find_one({
            "sheet_id": sheet_id,
            "row_number": actual_row_number
        })

        if not existing:
            sheet_rows_collection.insert_one({
                "org_id": org_id,
                "sheet_id": sheet_id,
                "sheet_name": sheet_name,
                "row_number": actual_row_number,
                "row_data": row_data,
                "synced_at": now,
            })
            rows_processed += 1
            new_last_row = actual_row_number

    # ── Update sheet doc: last_synced_at + last_processed_row_number ──────────
    if rows_processed > 0:
        db.google_sheets_connections.update_one(
            {"org_id": org_id, "sheet_id": sheet_id},
            {
                "$set": {
                    "last_synced_at": now,
                    "last_sync_rows": rows_processed,
                    "last_processed_row_number": new_last_row,
                    "updated_at": now.isoformat(),
                }
            }
        )
        logger.info(f"Synced {rows_processed} new rows for sheet {sheet_id} org {org_id}")

    return {"rows_processed": rows_processed}


async def _force_token_refresh(org_id: str) -> str:
    """Force a token refresh by clearing the expiry, then calling refresh_google_token."""
    db.google_connections.update_one(
        {"org_id": org_id},
        {"$set": {"credentials.token_expiry": "2000-01-01T00:00:00+00:00"}}
    )
    return await refresh_google_token(org_id)