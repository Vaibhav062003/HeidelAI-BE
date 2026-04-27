"""
google_sheets_routes.py  — UPDATED

Endpoints:
  GET    /api/google-sheets/available                               - list sheets from Google Drive
  POST   /api/google-sheets/connect                                 - connect a selected sheet
  DELETE /api/google-sheets/connections/{sheet_id}                  - remove a sheet
  GET    /api/google-sheets/connections                             - list connected sheets
  GET    /api/google-sheets/tabs?sheet_id=<id>                     - list worksheet tabs
  GET    /api/google-sheets/columns?sheet_id=<id>&tab_name=<tab>   - fetch column headers (auto-detect tab)
  POST   /api/google-sheets/manual-sync
  PATCH  /api/google-sheets/connections/{sheet_id}/toggle-polling
"""

import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional, List

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
    tab_name: Optional[str] = None  # None = auto-detect first tab


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

        existing_connection = db.google_sheets_connections.find_one({"org_id": org_id, "sheet_id": request.sheet_id})
        if existing_connection:
            return {
                "sheet_id": existing_connection.get("sheet_id", request.sheet_id),
                "sheet_name": existing_connection.get("sheet_name", request.sheet_name),
                "tab_name": existing_connection.get("tab_name") or request.tab_name or "Sheet1",
                "message": "Already connected."
            }

        # Auto-detect the first tab if none was provided
        tab_name = request.tab_name
        if not tab_name:
            try:
                access_token = await refresh_google_token(org_id)
                detected_tabs = await _fetch_sheet_tabs(request.sheet_id, access_token, org_id)
                tab_name = detected_tabs[0] if detected_tabs else "Sheet1"
            except Exception as e:
                logger.warning(f"Failed to auto-detect tab for sheet {request.sheet_id}: {e}. Defaulting to Sheet1.")
                tab_name = "Sheet1"

        now = datetime.now(timezone.utc)
        db.google_sheets_connections.insert_one({
            "org_id": org_id,
            "sheet_id": request.sheet_id,
            "sheet_name": request.sheet_name,
            "tab_name": tab_name,
            "last_synced_at": None,
            "last_sync_rows": 0,
            "polling_enabled": True,
            "last_processed_row_number": 0,  # 0 = never synced; first sync will auto-detect start row
            "header_row": None,  # Cached header row number (auto-detected on first sync)
            "col_offset": None,  # Cached column offset (auto-detected on first sync)
            "connected_at": now,
            "updated_at": now,
        })

        logger.info(f"Sheet {request.sheet_id} connected for org {org_id} (tab: {tab_name})")
        return {"sheet_id": request.sheet_id, "sheet_name": request.sheet_name, "tab_name": tab_name, "message": "Sheet connected successfully."}

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


# ─── NEW: List worksheet tabs for a sheet ─────────────────────────────────────

@router.get("/tabs")
async def get_sheet_tabs(
    user: CurrentUser,
    sheet_id: str = Query(..., description="Google Sheet ID to fetch tabs for"),
):
    """
    Fetch all worksheet tab names from a Google Sheet.
    Used by the automation builder to let users pick which tab to read from.

    Returns:
        { "tabs": ["Sheet1", "Form Responses 1", ...], "sheet_id": "..." }
    """
    try:
        org_id = user.org_id
        access_token = await refresh_google_token(org_id)
        tabs = await _fetch_sheet_tabs(sheet_id, access_token, org_id)
        return {"tabs": tabs, "sheet_id": sheet_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching tabs for sheet {sheet_id} org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch tabs: {str(e)}")


# ─── NEW: Get column headers for a sheet ──────────────────────────────────────

@router.get("/columns")
async def get_sheet_columns(
    user: CurrentUser,
    sheet_id: str = Query(..., description="Google Sheet ID to fetch columns for"),
    tab_name: Optional[str] = Query(None, description="Worksheet tab name (auto-detects if omitted)"),
    update_stored_tab: bool = Query(False, description="Whether to update the stored tab if a different one is used"),
):
    """
    Fetch the column headers (first row) from a connected Google Sheet.
    Used by the automation builder to let users choose a phone number column.

    If tab_name is provided, it reads from that specific tab.
    If tab_name is omitted, it tries the stored tab_name from the connection,
    then falls back to auto-detecting by trying all tabs until headers are found.

    Returns:
        { "columns": ["Name", "Email", "Phone Number", ...], "tab_name": "...", "available_tabs": [...] }
    """
    try:
        org_id = user.org_id

        # Verify this sheet is connected for this org
        sheet_doc = db.google_sheets_connections.find_one({"org_id": org_id, "sheet_id": sheet_id})
        if not sheet_doc:
            raise HTTPException(status_code=404, detail="Sheet not connected. Please connect it first.")

        # Get a valid access token
        access_token = await refresh_google_token(org_id)

        # Determine which tab(s) to try
        stored_tab = sheet_doc.get("tab_name", "Sheet1")
        explicit_tab = tab_name  # from query param

        # Fetch available tabs for fallback and response
        available_tabs: List[str] = []
        try:
            available_tabs = await _fetch_sheet_tabs(sheet_id, access_token, org_id)
        except Exception as e:
            logger.warning(f"Failed to fetch tabs for sheet {sheet_id}: {e}")

        # Build ordered list of tabs to try
        tabs_to_try: List[str] = []
        if explicit_tab:
            tabs_to_try = [explicit_tab]
        else:
            if stored_tab:
                tabs_to_try.append(stored_tab)
            # Add all available tabs that we haven't tried yet
            for t in available_tabs:
                if t not in tabs_to_try:
                    tabs_to_try.append(t)
            # Last resort fallback
            if not tabs_to_try:
                tabs_to_try = ["Sheet1"]

        # Try each tab until we get columns
        columns: List[str] = []
        used_tab = tabs_to_try[0]

        async with httpx.AsyncClient() as client:
            for try_tab in tabs_to_try:
                try:
                    fetched = await _fetch_columns_from_tab(sheet_id, try_tab, access_token, org_id, client)
                    if fetched:
                        columns = fetched
                        used_tab = try_tab
                        break
                except HTTPException as he:
                    if explicit_tab or he.status_code in [401, 403]:
                        raise
                    logger.debug(f"Tab '{try_tab}' failed during auto-detection: {he.detail}")
                    continue
                except Exception as e:
                    logger.debug(f"Tab '{try_tab}' failed for sheet {sheet_id}: {e}")
                    if explicit_tab:
                        raise HTTPException(status_code=500, detail=str(e))
                    continue

        # If we found columns on a different tab than stored, update the stored tab (only if opted-in)
        if columns and used_tab != stored_tab and update_stored_tab:
            try:
                db.google_sheets_connections.update_one(
                    {"org_id": org_id, "sheet_id": sheet_id},
                    {"$set": {"tab_name": used_tab, "updated_at": datetime.now(timezone.utc)}}
                )
                logger.info(f"Updated stored tab_name to '{used_tab}' for sheet {sheet_id} org {org_id}")
            except Exception as e:
                logger.warning(f"Failed to update tab_name for sheet {sheet_id}: {e}")

        logger.info(f"Fetched {len(columns)} columns from tab '{used_tab}' for sheet {sheet_id} org {org_id}")
        return {
            "columns": columns,
            "sheet_id": sheet_id,
            "tab_name": used_tab,
            "available_tabs": available_tabs,
        }

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


async def _fetch_sheet_tabs(sheet_id: str, access_token: str, org_id: str) -> List[str]:
    """
    Fetch all worksheet tab names from a Google Sheet using the Sheets API.
    Returns a list of tab title strings, e.g. ["Sheet1", "Form Responses 1"].
    """
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}?fields=sheets.properties.title"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if resp.status_code == 401:
            access_token = await _force_token_refresh(org_id)
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )

    if resp.status_code != 200:
        try:
            error_msg = resp.json().get("error", {}).get("message", "Unknown error")
        except Exception:
            error_msg = resp.text or "Unknown error"
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail=f"Google Sheets API error: {error_msg}")
        if resp.status_code == 403:
            raise HTTPException(status_code=403, detail=f"Google Sheets API error: {error_msg}")
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Google Sheets API error: {error_msg}")
        raise HTTPException(status_code=502, detail=f"Google Sheets API error: {error_msg}")

    sheets_data = resp.json().get("sheets", [])
    tabs = [
        sheet.get("properties", {}).get("title", "")
        for sheet in sheets_data
        if sheet.get("properties", {}).get("title")
    ]
    return tabs


async def _fetch_columns_from_tab(
    sheet_id: str, tab_name: str, access_token: str, org_id: str, client: Optional[httpx.AsyncClient] = None
) -> List[str]:
    """
    Smart column header detection: reads the first 20 rows and finds the first
    row containing 2+ non-empty cells. This handles:
      - Standard sheets where headers are in row 1
      - Google Sheets "Table" objects where headers start at row 2, 3, etc.
      - Tables that don't start at column A (leading empty cells are stripped)
      - Sheets with a title/label row above the actual header row

    Returns a list of non-empty header strings from the detected header row.
    """
    import urllib.parse
    encoded_tab = urllib.parse.quote(tab_name, safe="")
    # Fetch the first 20 rows to scan for headers
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{encoded_tab}!A1:ZZ20"
    )

    async def _do_request(c):
        nonlocal access_token
        r = await c.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        if r.status_code == 401:
            access_token = await _force_token_refresh(org_id)
            r = await c.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        return r

    if client:
        resp = await _do_request(client)
    else:
        async with httpx.AsyncClient() as _client:
            resp = await _do_request(_client)

    if resp.status_code != 200:
        try:
            error_payload = resp.json()
        except Exception:
            error_payload = {}
        error_message = error_payload.get("error", {}).get("message") or resp.text or "Google Sheets API request failed"
        
        if resp.status_code == 403:
            raise HTTPException(status_code=403, detail=f"Access to the Google Sheet or tab was denied: {error_message}")
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Google Sheet or tab not found: {error_message}")
        if resp.status_code == 400:
            raise HTTPException(status_code=400, detail=f"Bad request, possibly invalid tab name: {error_message}")
        raise HTTPException(status_code=502, detail=f"Google Sheets API request failed with status {resp.status_code}: {error_message}")

    all_rows = resp.json().get("values", [])
    if not all_rows:
        return []

    # Find the header row: first row with 2+ non-empty cells
    for row in all_rows:
        non_empty = [cell for cell in row if cell and str(cell).strip()]
        if len(non_empty) >= 2:
            return non_empty

    # Fallback: if no row has 2+ values, return the first non-empty row
    for row in all_rows:
        non_empty = [cell for cell in row if cell and str(cell).strip()]
        if non_empty:
            return non_empty

    return []


async def _detect_header_row(
    sheet_id: str, tab_name: str, access_token: str, org_id: str
) -> tuple:
    """
    Detect the header row in a Google Sheet tab by scanning the first 20 rows.
    Returns (header_list, header_row_number_1_indexed, col_offset).

    - header_list: list of non-empty header strings
    - header_row_number: 1-indexed row number in the sheet (e.g., 3 means row 3)
    - col_offset: 0-indexed column where headers start (e.g., 1 means columns start at B)

    Used internally by _fetch_headers and _fetch_rows to support tables that
    don't start at A1.
    """
    import urllib.parse
    encoded_tab = urllib.parse.quote(tab_name, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{encoded_tab}!A1:ZZ20"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
        )
        if resp.status_code != 200:
            return ([], 1, 0)

    all_rows = resp.json().get("values", [])
    if not all_rows:
        return ([], 1, 0)

    # Find header row: first row with 2+ non-empty cells
    for row_idx, row in enumerate(all_rows):
        non_empty = [cell for cell in row if cell and str(cell).strip()]
        if len(non_empty) >= 2:
            # Determine column offset (skip leading empty cells)
            col_offset = 0
            for cell in row:
                if cell and str(cell).strip():
                    break
                col_offset += 1
            headers = [str(cell).strip() for cell in row[col_offset:] if cell and str(cell).strip()]
            return (headers, row_idx + 1, col_offset)

    # Fallback: first row with any data
    for row_idx, row in enumerate(all_rows):
        non_empty = [cell for cell in row if cell and str(cell).strip()]
        if non_empty:
            col_offset = 0
            for cell in row:
                if cell and str(cell).strip():
                    break
                col_offset += 1
            headers = [str(cell).strip() for cell in row[col_offset:] if cell and str(cell).strip()]
            return (headers, row_idx + 1, col_offset)

    return ([], 1, 0)



async def refresh_google_token(org_id: str) -> str:
   
    connection = await asyncio.to_thread(
        db.google_connections.find_one, {"org_id": org_id, "status": "active"}
    )
    if not connection:
        raise HTTPException(status_code=404, detail=f"No active Google connection for org {org_id}")

    credentials = connection.get("credentials", {})
    access_token = credentials.get("access_token")
    refresh_token = credentials.get("refresh_token")
    token_expiry_value = credentials.get("token_expiry")

    if token_expiry_value and access_token:
        try:
            token_expiry = None
            if isinstance(token_expiry_value, datetime):
                token_expiry = token_expiry_value
            elif isinstance(token_expiry_value, str):
                token_expiry = datetime.fromisoformat(token_expiry_value.replace("Z", "+00:00"))

            if token_expiry is not None:
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

    last_row = sheet_doc.get("last_processed_row_number", 0)
    sheet_name = sheet_doc.get("sheet_name", "Sheet1")
    tab_name = sheet_doc.get("tab_name", "Sheet1")
    
    import urllib.parse
    encoded_tab = urllib.parse.quote(tab_name, safe="")
    access_token = await refresh_google_token(org_id)

    # --- Header detection (cached after first detection) ---
    cached_header_row = sheet_doc.get("header_row")
    cached_col_offset = sheet_doc.get("col_offset")

    if cached_header_row is not None and cached_col_offset is not None:
        header_row = cached_header_row
        col_offset = cached_col_offset
        # Fetch just the header row to extract column names
        header_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
            f"/values/{encoded_tab}!A{header_row}:ZZ{header_row}"
        )
        async with httpx.AsyncClient() as client:
            h_resp = await client.get(
                header_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
            )
        if h_resp.status_code == 200:
            h_vals = h_resp.json().get("values", [[]])
            h_row = h_vals[0] if h_vals else []
            headers = [str(c).strip() for c in h_row[col_offset:] if c and str(c).strip()]
        else:
            headers = []
    else:
        # First sync or cache missing — detect headers (scans first 20 rows)
        headers, header_row, col_offset = await _detect_header_row(
            sheet_id, tab_name, access_token, org_id
        )
        if headers:
            try:
                await asyncio.to_thread(
                    db.google_sheets_connections.update_one,
                    {"org_id": org_id, "sheet_id": sheet_id},
                    {"$set": {"header_row": header_row, "col_offset": col_offset}}
                )
            except Exception as e:
                logger.warning(f"[SyncSheet] Failed to cache header info: {e}")

    if not headers:
        logger.warning(f"[SyncSheet] No headers found for sheet {sheet_id} org {org_id} — skipping sync")
        return {"rows_processed": 0}

    # Data starts after header row
    start_row = max(last_row, header_row) + 1
    end_row = start_row + 999
    sheets_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{encoded_tab}!A{start_row}:ZZ{end_row}"

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

    rows_processed = 0
    now = datetime.now(timezone.utc)
    new_last_row = last_row
    collection = db[f"google_sheet_rows_{org_id}"]

    for idx, row_values in enumerate(values):
        actual_row = start_row + idx
        
        # Account for column offset
        data_cells = row_values[col_offset:] if col_offset < len(row_values) else row_values
        row_data = {
            (headers[i] if i < len(headers) else f"col_{i+1}"): v
            for i, v in enumerate(data_cells)
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