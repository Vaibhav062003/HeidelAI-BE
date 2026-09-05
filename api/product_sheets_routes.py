import asyncio
import httpx
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_mongo_db
from auth.dependencies import CurrentUser
from loguru import logger

from api.google_sheets_routes import (
    refresh_google_token,
    _force_token_refresh,
    _detect_header_row,
    _fetch_sheet_tabs,
    _fetch_columns_from_tab,
)
from services.product_sheet_sync import auto_suggest_mapping, auto_detect_product_type, sync_product_sheet
from schemas.models import (
    PreviewProductSheetRequest,
    ConnectProductSheetRequest,
    SyncProductSheetRequest,
)

db = get_mongo_db()

router = APIRouter(prefix="/api/product-sheets", tags=["Product Sheets"])


# ─── POST /preview ─────────────────────────────────────────────────────────────


@router.post("/preview")
async def preview_product_sheet(request: PreviewProductSheetRequest, user: CurrentUser):
    """Preview columns, sample rows, and auto-suggested mapping for a Google Sheet."""
    try:
        org_id = user.org_id

        # Verify active Google connection
        conn = await asyncio.to_thread(
            db.google_connections.find_one, {"org_id": org_id, "status": "active"}
        )
        if not conn:
            raise HTTPException(status_code=404, detail="No active Google connection found.")

        access_token = await refresh_google_token(org_id)

        # Resolve tab name
        tab_name = request.tab_name
        available_tabs = []
        try:
            available_tabs = await _fetch_sheet_tabs(request.sheet_id, access_token, org_id)
        except Exception as e:
            logger.warning(f"Failed to fetch tabs for sheet {request.sheet_id}: {e}")

        if not tab_name:
            tab_name = available_tabs[0] if available_tabs else "Sheet1"

        # Fetch columns (headers)
        columns = await _fetch_columns_from_tab(request.sheet_id, tab_name, access_token, org_id)
        if not columns:
            raise HTTPException(status_code=400, detail="No columns found in the sheet.")

        # Detect header row for data fetch
        headers, header_row, col_offset = await _detect_header_row(
            request.sheet_id, tab_name, access_token, org_id
        )

        # Fetch first 5 data rows for preview
        sample_rows = []
        if headers:
            encoded_tab = urllib.parse.quote(tab_name, safe="")
            data_start_row = header_row + 1
            range_str = f"{encoded_tab}!A{data_start_row}:ZZ{data_start_row + 4}"
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{request.sheet_id}/values/{range_str}"

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
                )
                if resp.status_code == 401:
                    access_token = await _force_token_refresh(org_id)
                    resp = await client.get(
                        url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
                    )

            if resp.status_code == 200:
                raw_rows = resp.json().get("values", [])
                for raw_row in raw_rows:
                    adjusted_row = raw_row[col_offset:] if col_offset > 0 else raw_row
                    row_dict = {}
                    for i, header in enumerate(headers):
                        if i < len(adjusted_row):
                            row_dict[header] = adjusted_row[i]
                        else:
                            row_dict[header] = ""
                    sample_rows.append(row_dict)

        # Auto-suggest mapping and detect product type from columns
        suggested_mapping = auto_suggest_mapping(columns)
        detected_product_type = auto_detect_product_type(columns)

        logger.info(
            f"Product sheet preview for org {org_id} sheet {request.sheet_id}: "
            f"{len(columns)} columns, {len(sample_rows)} sample rows, "
            f"{len(suggested_mapping)} auto-mapped fields, detected_type={detected_product_type}"
        )

        return {
            "columns": columns,
            "sample_rows": sample_rows,
            "suggested_mapping": suggested_mapping,
            "available_tabs": available_tabs,
            "detected_product_type": detected_product_type,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error previewing product sheet {request.sheet_id} for org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to preview sheet: {str(e)}")


# ─── POST /connect ─────────────────────────────────────────────────────────────


@router.post("/connect")
async def connect_product_sheet(request: ConnectProductSheetRequest, user: CurrentUser):
    """Save column mapping and trigger initial product sync."""
    try:
        org_id = user.org_id

        # Check for existing connection
        existing = await asyncio.to_thread(
            db.product_sheet_connections.find_one,
            {"org_id": org_id, "sheet_id": request.sheet_id}
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Sheet {request.sheet_id} is already connected. Disconnect it first."
            )

        # Ensure unique index on (org_id, sheet_id)
        await asyncio.to_thread(
            lambda: db.product_sheet_connections.create_index(
                [("org_id", 1), ("sheet_id", 1)], unique=True
            )
        )

        now = datetime.now(timezone.utc)
        has_primary = await asyncio.to_thread(
            db.product_sheet_connections.find_one,
            {"org_id": org_id, "status": "active", "is_primary": True},
        )
        connection_doc = {
            "org_id": org_id,
            "sheet_id": request.sheet_id,
            "sheet_name": request.sheet_name,
            "tab_name": request.tab_name,
            "column_mapping": request.column_mapping,
            "product_type": request.product_type,
            "last_synced_at": None,
            "last_sync_count": 0,
            "sync_errors": [],
            "status": "active",
            "is_primary": not bool(has_primary),
            "connected_at": now,
            "updated_at": now,
        }

        await asyncio.to_thread(db.product_sheet_connections.insert_one, connection_doc)
        logger.info(f"Product sheet {request.sheet_id} connected for org {org_id}")

        # Trigger initial sync
        sync_result = await sync_product_sheet(org_id, request.sheet_id)

        return {
            "status": "connected",
            "sheet_id": request.sheet_id,
            "sheet_name": request.sheet_name,
            "tab_name": request.tab_name,
            **sync_result,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting product sheet {request.sheet_id} for org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to connect product sheet: {str(e)}")


# ─── POST /sync ────────────────────────────────────────────────────────────────


@router.post("/sync")
async def manual_sync_product_sheet(request: SyncProductSheetRequest, user: CurrentUser):
    """Manually trigger a full reconciliation sync for a connected product sheet."""
    try:
        org_id = user.org_id

        # Validate connection exists
        connection = await asyncio.to_thread(
            db.product_sheet_connections.find_one,
            {"org_id": org_id, "sheet_id": request.sheet_id}
        )
        if not connection:
            raise HTTPException(status_code=404, detail="Product sheet connection not found.")

        sync_result = await sync_product_sheet(org_id, request.sheet_id)
        return sync_result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing product sheet {request.sheet_id} for org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync product sheet: {str(e)}")


# ─── GET /connections ──────────────────────────────────────────────────────────


@router.get("/connections")
async def list_product_sheet_connections(user: CurrentUser):
    """List all connected product sheets for the org."""
    try:
        org_id = user.org_id

        connections = await asyncio.to_thread(
            lambda: list(db.product_sheet_connections.find(
                {"org_id": org_id},
                {
                    "_id": 0,
                    "sheet_id": 1,
                    "sheet_name": 1,
                    "tab_name": 1,
                    "column_mapping": 1,
                    "last_synced_at": 1,
                    "last_sync_count": 1,
                    "sync_errors": 1,
                    "status": 1,
                    "is_primary": 1,
                }
            ).sort("connected_at", -1))
        )

        # Convert datetime fields to ISO strings for frontend
        for conn in connections:
            if conn.get("last_synced_at") and isinstance(conn["last_synced_at"], datetime):
                conn["last_synced_at"] = conn["last_synced_at"].isoformat()

        return {"connections": connections}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing product sheet connections for org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list product sheet connections: {str(e)}")


@router.put("/connections/{sheet_id}/primary")
async def set_primary_product_sheet(sheet_id: str, user: CurrentUser):
    """Choose the only Sheet that inventory edits may write back to."""
    if user.role not in {"admin", "org:admin"}:
        raise HTTPException(status_code=403, detail="Only organization admins can change the primary stock sheet")
    connection = await asyncio.to_thread(
        db.product_sheet_connections.find_one,
        {"org_id": user.org_id, "sheet_id": sheet_id, "status": "active"},
    )
    if not connection:
        raise HTTPException(status_code=404, detail="Active product sheet connection not found")
    now = datetime.now(timezone.utc)
    await asyncio.to_thread(
        db.product_sheet_connections.update_many,
        {"org_id": user.org_id},
        {"$set": {"is_primary": False, "updated_at": now}},
    )
    await asyncio.to_thread(
        db.product_sheet_connections.update_one,
        {"org_id": user.org_id, "sheet_id": sheet_id},
        {"$set": {"is_primary": True, "updated_at": now}},
    )
    return {"status": "success", "sheet_id": sheet_id}


# ─── DELETE /connections/{sheet_id} ────────────────────────────────────────────


@router.delete("/connections/{sheet_id}")
async def disconnect_product_sheet(sheet_id: str, user: CurrentUser):
    """Disconnect a product sheet and delete all products sourced from it."""
    try:
        org_id = user.org_id

        # Verify connection exists
        connection = await asyncio.to_thread(
            db.product_sheet_connections.find_one,
            {"org_id": org_id, "sheet_id": sheet_id}
        )
        if not connection:
            raise HTTPException(status_code=404, detail="Product sheet connection not found.")

        # Delete the connection
        await asyncio.to_thread(
            db.product_sheet_connections.delete_one,
            {"org_id": org_id, "sheet_id": sheet_id}
        )

        # 1. Unlink: Remove this sheet from all products' source arrays
        products_col = db[f"products_{org_id}"]
        await asyncio.to_thread(
            lambda: products_col.update_many(
                {"source_sheet_ids": sheet_id},
                {
                    "$pull": {"source_sheet_ids": sheet_id},
                    "$unset": {
                        f"source_rows.{sheet_id}": "",
                        f"source_values.{sheet_id}": "",
                    },
                }
            )
        )

        # 2. Garbage Collect: Delete products that are now orphaned (no active sources)
        delete_result = await asyncio.to_thread(
            lambda: products_col.delete_many({"source_sheet_ids": {"$size": 0}})
        )
        
        # 3. Clean up any legacy products that still use the scalar field
        legacy_delete_result = await asyncio.to_thread(
            lambda: products_col.delete_many({"source_sheet_id": sheet_id})
        )
        
        products_removed = delete_result.deleted_count + legacy_delete_result.deleted_count

        # Update org metadata product count
        total_all_products = await asyncio.to_thread(
            lambda: products_col.count_documents({})
        )
        now = datetime.now(timezone.utc)
        await asyncio.to_thread(
            db.organizations_metadata.update_one,
            {"org_id": org_id},
            {"$set": {
                "product_info.products_uploaded": total_all_products > 0,
                "product_info.products_count": total_all_products,
                "product_info.last_updated_at": now,
            }},
            upsert=True
        )

        logger.info(
            f"Product sheet {sheet_id} disconnected for org {org_id}, "
            f"{products_removed} products removed"
        )

        return {
            "message": f"Product sheet disconnected. {products_removed} product(s) removed.",
            "products_removed": products_removed,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error disconnecting product sheet {sheet_id} for org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to disconnect product sheet: {str(e)}")


# ─── PUT /connections/{sheet_id}/mapping ───────────────────────────────────────


class UpdateMappingRequest(BaseModel):
    column_mapping: Dict[str, str]


@router.put("/connections/{sheet_id}/mapping")
async def update_product_sheet_mapping(sheet_id: str, request: UpdateMappingRequest, user: CurrentUser):
    """Update the column mapping for a connected product sheet."""
    try:
        org_id = user.org_id

        # Verify connection exists
        connection = await asyncio.to_thread(
            db.product_sheet_connections.find_one,
            {"org_id": org_id, "sheet_id": sheet_id}
        )
        if not connection:
            raise HTTPException(status_code=404, detail="Product sheet connection not found.")

        now = datetime.now(timezone.utc)
        await asyncio.to_thread(
            db.product_sheet_connections.update_one,
            {"org_id": org_id, "sheet_id": sheet_id},
            {"$set": {
                "column_mapping": request.column_mapping,
                "updated_at": now,
            }}
        )

        logger.info(f"Column mapping updated for product sheet {sheet_id} org {org_id}")

        return {
            "message": "Mapping updated",
            "sheet_id": sheet_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating mapping for product sheet {sheet_id} org {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update mapping: {str(e)}")
