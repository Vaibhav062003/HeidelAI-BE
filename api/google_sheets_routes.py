"""
google_sheets_routes.py  — UPDATED

Endpoints:
  GET    /api/google-sheets/available                               - list sheets from Google Drive
  POST   /api/google-sheets/connect                                 - connect a selected sheet
  DELETE /api/google-sheets/connections/{sheet_id}                  - remove a sheet
  GET    /api/google-sheets/connections                             - list connected sheets
  GET    /api/google-sheets/columns?sheet_id=<id>                  - fetch column headers (NEW)
  POST   /api/google-sheets/manual-sync
  PATCH  /api/google-sheets/connections/{sheet_id}/toggle-polling
"""

import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from database import get_mongo_db
from auth.dependencies import CurrentUser
from config.settings import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
from loguru import logger
import asyncio

db = get_mongo_db()

router = APIRouter(prefix="/api/google-sheets", tags=["Google Sheets"])


# ─── Pydantic Models ───────────────────────────────────────────────────────────

class ManualSyncRequest(BaseModel):
    sheet_id: str

class ConnectSheetRequest(BaseModel):
    sheet_id: str
    sheet_name: str
    tab_name: str = "Sheet1"


# ─── NEW: List available sheets from Google Drive ──────────────────────────────

@router.get("/available")
async def list_available_sheets(user: CurrentUser):
    """
    Fetch all Google Sheets the connected account can access via Drive API.
    Also marks which ones are already connected to this org.
    """
    try:
        org_id = user.org_id
        access_token = await refresh_google_token(org_id)

        async with httpx.AsyncClient() as client:
            drive_response = await client.get(
                "https://www.googleapis.com/drive/v3/files",
                params={
                    "q": "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                    "fields": "files(id,name,modifiedTime)",
                    "orderBy": "modifiedTime desc",
                    "pageSize": 100,
                },
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15
            )

            if drive_response.status_code == 401:
                access_token = await _force_token_refresh(org_id)
                drive_response = await client.get(
                    "https://www.googleapis.com/drive/v3/files",
                    params={
                        "q": "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                        "fields": "files(id,name,modifiedTime)",
                        "orderBy": "modifiedTime desc",
                        "pageSize": 100,
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=15
                )

        if drive_response.status_code != 200:
            error_msg = drive_response.json().get("error", {}).get("message", "Unknown error")
            raise HTTPException(status_code=502, detail=f"Google Drive API error: {error_msg}")

        files = drive_response.json().get("files", [])

        connected_ids = set(
            doc["sheet_id"]
            for doc in db.google_sheets_connections.find({"org_id": org_id}, {"sheet_id": 1})
        )

        sheets = [
            {
                "sheet_id": f["id"],
                "sheet_name": f["name"],
                "modified_at": f.get("modifiedTime"),
                "is_connected": f["id"] in connected_ids,
            }
            for f in files
        ]

        return {"sheets": sheets}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing available sheets for org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list sheets: {str(e)}")


# ─── NEW: Connect a sheet ──────────────────────────────────────────────────────

@router.post("/connect")
async def connect_sheet(request: ConnectSheetRequest, user: CurrentUser):
    """Connect a Google Sheet for syncing (creates entry in google_sheets_connections)."""
    try:
        org_id = user.org_id

        if not db.google_connections.find_one({"org_id": org_id, "status": "active"}):
            raise HTTPException(status_code=404, detail="No active Google connection found.")

        if db.google_sheets_connections.find_one({"org_id": org_id, "sheet_id": request.sheet_id}):
            return {"sheet_id": request.sheet_id, "sheet_name": request.sheet_name, "message": "Already connected."}

        now = datetime.now(timezone.utc)
        db.google_sheets_connections.insert_one({
            "org_id": org_id,
            "sheet_id": request.sheet_id,
            "sheet_name": request.sheet_name,
            "tab_name": request.tab_name,
            "last_synced_at": None,
            "last_sync_rows": 0,
            "polling_enabled": True,
            "last_processed_row_number": 1,
            "connected_at": now,
            "updated_at": now,
        })

        logger.info(f"Sheet {request.sheet_id} connected for org {org_id}")
        return {"sheet_id": request.sheet_id, "sheet_name": request.sheet_name, "message": "Sheet connected successfully."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting sheet {request.sheet_id} for org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to connect sheet: {str(e)}")


# ─── NEW: Disconnect (remove) a sheet ─────────────────────────────────────────

@router.delete("/connections/{sheet_id}")
async def disconnect_sheet(sheet_id: str, user: CurrentUser):
    """Remove a sheet from google_sheets_connections — stops all syncing for it."""
    try:
        org_id = user.org_id
        result = db.google_sheets_connections.delete_one({"org_id": org_id, "sheet_id": sheet_id})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Sheet not found.")
        logger.info(f"Sheet {sheet_id} removed for org {org_id}")
        return {"message": "Sheet disconnected successfully."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error disconnecting sheet {sheet_id} for org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to disconnect sheet: {str(e)}")


# ─── Existing: List connected sheets ──────────────────────────────────────────

@router.get("/connections")
async def list_google_sheets_connections(user: CurrentUser):
    try:
        org_id = user.org_id

        if not db.google_connections.find_one({"org_id": org_id, "status": "active"}):
            raise HTTPException(status_code=404, detail="No active Google connection found.")

        sheets = list(db.google_sheets_connections.find(
            {"org_id": org_id},
            {"_id": 0, "sheet_id": 1, "sheet_name": 1, "last_synced_at": 1,
             "last_sync_rows": 1, "polling_enabled": 1, "last_processed_row_number": 1}
        ).sort("connected_at", -1))

        for sheet in sheets:
            if sheet.get("last_synced_at") and isinstance(sheet["last_synced_at"], datetime):
                sheet["last_synced_at"] = sheet["last_synced_at"].isoformat()

        return {"connections": sheets}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing connections for org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list connected sheets: {str(e)}")


# ─── NEW: Get column headers for a sheet ──────────────────────────────────────

@router.get("/columns")
async def get_sheet_columns(
    user: CurrentUser,
    sheet_id: str = Query(..., description="Google Sheet ID to fetch columns for"),
):
    """
    Fetch the column headers (first row) from a connected Google Sheet.
    Used by the automation builder to let users choose a phone number column.

    Returns:
        { "columns": ["Name", "Email", "Phone Number", ...] }
    """
    try:
        org_id = user.org_id

        # Verify this sheet is connected for this org
        sheet_doc = db.google_sheets_connections.find_one({"org_id": org_id, "sheet_id": sheet_id})
        if not sheet_doc:
            raise HTTPException(status_code=404, detail="Sheet not connected. Please connect it first.")

        tab_name = sheet_doc.get("tab_name", "Sheet1")

        # Get a valid access token
        access_token = await refresh_google_token(org_id)

        async with httpx.AsyncClient() as client:
            def _get_headers_url():
                return (
                    f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
                    f"/values/{tab_name}!A1:ZZ1"
                )

            resp = await client.get(
                _get_headers_url(),
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15
            )
            if resp.status_code == 401:
                access_token = await _force_token_refresh(org_id)
                resp = await client.get(
                    _get_headers_url(),
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=15
                )

        if resp.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="Spreadsheet not found. It may have been deleted or access revoked."
            )

        if resp.status_code != 200:
            error_msg = resp.json().get("error", {}).get("message", "Unknown error")
            raise HTTPException(status_code=502, detail=f"Google Sheets API error: {error_msg}")

        values = resp.json().get("values", [[]])
        columns = values[0] if values else []

        # Filter out empty column names
        columns = [col for col in columns if col and str(col).strip()]

        logger.info(f"Fetched {len(columns)} columns for sheet {sheet_id} org {org_id}")
        return {"columns": columns, "sheet_id": sheet_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching columns for sheet {sheet_id} org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch columns: {str(e)}")


# ─── Existing: Manual sync ─────────────────────────────────────────────────────

@router.post("/manual-sync")
async def manual_sync_sheet(request: ManualSyncRequest, user: CurrentUser):
    try:
        org_id = user.org_id
        if not request.sheet_id:
            raise HTTPException(status_code=400, detail="sheet_id is required")
        if not db.google_sheets_connections.find_one({"org_id": org_id, "sheet_id": request.sheet_id}):
            raise HTTPException(status_code=404, detail="Sheet not found or not connected")

        result = await _sync_sheet(org_id, request.sheet_id)
        rows_processed = result.get("rows_processed", 0)
        if rows_processed == 0:
            return {"status": "no_new_data", "rows_processed": 0}

        # Trigger google_sheet automations for this sheet (Manual Sync)
        try:
            from services.google_sheet_automation import process_new_sheet_rows
           
            asyncio.create_task(process_new_sheet_rows(org_id, request.sheet_id))
        except Exception as e:
            logger.warning(f"Could not trigger google_sheet automations after manual sync: {e}")

        return {"status": "synced", "rows_processed": rows_processed}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual sync error for {request.sheet_id} org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


# ─── Existing: Toggle polling ──────────────────────────────────────────────────

@router.patch("/connections/{sheet_id}/toggle-polling")
async def toggle_sheet_polling(sheet_id: str, user: CurrentUser):
    try:
        org_id = user.org_id
        sheet_doc = db.google_sheets_connections.find_one({"org_id": org_id, "sheet_id": sheet_id})
        if not sheet_doc:
            raise HTTPException(status_code=404, detail="Sheet not found")

        new_state = not sheet_doc.get("polling_enabled", True)
        db.google_sheets_connections.update_one(
            {"org_id": org_id, "sheet_id": sheet_id},
            {"$set": {"polling_enabled": new_state, "updated_at": datetime.now(timezone.utc)}}
        )
        return {"sheet_id": sheet_id, "polling_enabled": new_state}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling polling for {sheet_id} org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to toggle polling: {str(e)}")


# ─── Internal Helpers ──────────────────────────────────────────────────────────

async def refresh_google_token(org_id: str) -> str:
   
    connection = await asyncio.to_thread(
        db.google_connections.find_one, {"org_id": org_id, "status": "active"}
    )
    if not connection:
        raise HTTPException(status_code=404, detail=f"No active Google connection for org {org_id}")

    credentials = connection.get("credentials", {})
    access_token = credentials.get("access_token")
    refresh_token = credentials.get("refresh_token")
    token_expiry_str = credentials.get("token_expiry")

    if token_expiry_str and access_token:
        try:
            token_expiry = datetime.fromisoformat(token_expiry_str)
            if token_expiry.tzinfo is None:
                token_expiry = token_expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) + timedelta(minutes=5) < token_expiry:
                return access_token
        except Exception:
            pass

    if not refresh_token:
        raise HTTPException(status_code=401, detail="Google refresh token missing. Please reconnect.")

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        logger.error("GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not configured.")
        raise HTTPException(status_code=500, detail="Google OAuth credentials not configured on server.")

    async with httpx.AsyncClient() as client:
        token_response = await client.post(
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
        if error_info.get("error") in ("invalid_grant", "token_expired"):
            await asyncio.to_thread(
                db.google_connections.update_one,
                {"org_id": org_id},
                {"$set": {"status": "inactive", "updated_at": datetime.now(timezone.utc)}}
            )
        raise HTTPException(status_code=401, detail=f"Token refresh failed: {error_info.get('error_description', '')}")

    token_info = token_response.json()
    new_access_token = token_info["access_token"]
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=token_info.get("expires_in", 3600))

    await asyncio.to_thread(
        db.google_connections.update_one,
        {"org_id": org_id},
        {"$set": {
            "credentials.access_token": new_access_token,
            "credentials.token_expiry": new_expiry.isoformat(),
            "updated_at": datetime.now(timezone.utc),
        }}
    )
    return new_access_token


async def _force_token_refresh(org_id: str) -> str:
  
    await asyncio.to_thread(
        db.google_connections.update_one,
        {"org_id": org_id},
        {"$set": {"credentials.token_expiry": "2000-01-01T00:00:00+00:00", "updated_at": datetime.now(timezone.utc)}}
    )
    return await refresh_google_token(org_id)


async def _sync_sheet(org_id: str, sheet_id: str) -> dict:

    sheet_doc = await asyncio.to_thread(
        db.google_sheets_connections.find_one, {"org_id": org_id, "sheet_id": sheet_id}
    )
    if not sheet_doc:
        raise HTTPException(status_code=404, detail=f"Sheet {sheet_id} not found for org {org_id}")

    last_row = sheet_doc.get("last_processed_row_number", 1)
    sheet_name = sheet_doc.get("sheet_name", "Sheet1")
    tab_name = sheet_doc.get("tab_name", "Sheet1")
    access_token = await refresh_google_token(org_id)

    start_row = last_row + 1
    end_row = start_row + 999
    sheets_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{tab_name}!A{start_row}:ZZ{end_row}"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            sheets_url, 
            headers={"Authorization": f"Bearer {access_token}"}, 
            timeout=15
        )
        if response.status_code == 401:
            access_token = await _force_token_refresh(org_id)
            response = await client.get(
                sheets_url, 
                headers={"Authorization": f"Bearer {access_token}"}, 
                timeout=15
            )

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Spreadsheet not found. It may have been deleted or access revoked.")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Google Sheets API error: {response.json().get('error', {}).get('message', 'Unknown')}")

    values = response.json().get("values", [])
    if not values:
        return {"rows_processed": 0}

    async with httpx.AsyncClient() as client:
        headers_resp = await client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{tab_name}!A1:ZZ1",
            headers={"Authorization": f"Bearer {access_token}"}, 
            timeout=10
        )
    headers = []
    if headers_resp.status_code == 200:
        hv = headers_resp.json().get("values", [[]])
        headers = hv[0] if hv else []

    rows_processed = 0
    now = datetime.now(timezone.utc)
    new_last_row = last_row
    collection = db[f"google_sheet_rows_{org_id}"]

    for idx, row_values in enumerate(values):
        actual_row = start_row + idx
        row_data = {
            (headers[i] if i < len(headers) else f"col_{i+1}"): v
            for i, v in enumerate(row_values)
        }
        result = await asyncio.to_thread(
            collection.update_one,
            {"sheet_id": sheet_id, "row_number": actual_row},
            {
                "$setOnInsert": {
                    "org_id": org_id,
                    "sheet_id": sheet_id,
                    "sheet_name": sheet_name,
                    "row_number": actual_row,
                    "row_data": row_data,
                    "synced_at": now,
                }
            },
            upsert=True,
        )
        if result.upserted_id is not None:
            rows_processed += 1
            new_last_row = actual_row

    if rows_processed > 0:
        await asyncio.to_thread(
            db.google_sheets_connections.update_one,
            {"org_id": org_id, "sheet_id": sheet_id},
            {"$set": {
                "last_synced_at": now, "last_sync_rows": rows_processed,
                "last_processed_row_number": new_last_row,
                "updated_at": now,
            }}
        )

    return {"rows_processed": rows_processed}