import asyncio
import httpx
import urllib.parse
from datetime import datetime, timezone

from pymongo import UpdateOne
from loguru import logger

from database import get_mongo_db
from api.google_sheets_routes import refresh_google_token, _force_token_refresh, _detect_header_row


# ─── Field Aliases ─────────────────────────────────────────────────────────────
# Maps canonical MongoDB field names to possible sheet column name variations.
# Used by auto_suggest_mapping() for the preview endpoint. The transform_row()
# function does direct 1:1 snake_case mapping, so columns that already match
# the MongoDB field name (e.g. "brand" → "brand") don't need aliases here —
# aliases are only needed when the sheet column name differs from the DB field.

FIELD_ALIASES = {
    # --- Shared / Laptop fields ---
    "product_name": ["product name", "product_name", "name", "title", "product title", "item name", "item"],
    "model_no":     ["model_no", "model no", "model number", "model", "sku", "item code", "product code", "part number", "part no", "article no"],
    "description":  ["description", "desc", "product description", "details", "short description"],
    "price":        ["price", "mrp", "cost", "rate", "unit price", "selling price", "amount"],
    "stock":        ["stock", "quantity", "qty", "available", "inventory", "units", "available qty"],
    "image_url":    ["image", "image url", "image_url", "image link", "photo", "picture", "product image"],
    # --- Component-specific aliases (where sheet column ≠ MongoDB field) ---
    "brand":                         ["brand", "manufacturer", "make"],
    "model_name":                    ["model_name", "model name"],
    "graphics_card":                 ["graphics_card", "graphics card", "gpu"],
    "graphics_card_space_gb":        ["graphics_card_space_gb", "graphics card space gb", "gpu memory", "vram"],
    "ram_gb":                        ["ram_gb", "ram", "ram gb", "memory", "memory gb"],
    "storage_gb":                    ["storage_gb", "ssd", "storage", "storage gb", "ssd gb"],
    "ssd_type":                      ["ssd_type", "ssd type", "ssd_t", "sd_type", "storage type"],
    "hdd":                           ["hdd", "hard disk", "hard drive"],
    "smps":                          ["smps", "power supply", "psu"],
    "processor":                     ["processor", "cpu", "chip"],
    "processor_series":              ["processor_series", "processor series", "cpu series"],
    "motherboard_series":            ["motherboard_series", "motherboard series", "motherboard_d_series", "motherboard d series"],
    "motherboard_processor_support": ["motherboard_processor_support", "motherboard processor support", "motherboard_processor_sup_port", "motherboard processor sup port"],
    "cabinet":                       ["cabinet", "case", "chassis"],
    "category":                      ["category", "product category", "product type"],
    "in_stock":                      ["in_stock", "in stock"],
}

CANONICAL_FIELDS = set(FIELD_ALIASES.keys())

# Columns that, if present, strongly indicate this is a component sheet
COMPONENT_INDICATORS = {
    "processor_series", "motherboard_series", "motherboard_processor_support",
    "cabinet", "smps", "hdd", "ssd_type", "graphics_card", "ram",
    "processor", "gpu", "cpu", "psu",
}



# ─── Helper Functions ──────────────────────────────────────────────────────────


def auto_suggest_mapping(columns: list[str]) -> dict[str, str]:
    """Auto-map sheet columns to product fields using FIELD_ALIASES.
    Returns {product_field: sheet_column_name}.
    Unmapped columns are NOT included (they go to metadata automatically)."""
    mapping = {}
    used_columns = set()
    for field, aliases in FIELD_ALIASES.items():
        for col in columns:
            clean_col = col.strip().lower().replace(" ", "_")
            if clean_col in [a.replace(" ", "_") for a in aliases] or col.strip().lower() in aliases:
                if col not in used_columns:
                    mapping[field] = col
                    used_columns.add(col)
                    break

    # Fallback for required field: product_name
    if "product_name" not in mapping:
        for col in columns:
            if col not in used_columns and ("name" in col.lower() or "title" in col.lower() or "product" in col.lower()):
                mapping["product_name"] = col
                used_columns.add(col)
                break
        if "product_name" not in mapping and columns:
            # Absolute fallback to the first string-like column or just the first column
            for col in columns:
                if col not in used_columns:
                    mapping["product_name"] = col
                    used_columns.add(col)
                    break

    # Fallback for required field: model_no
    if "model_no" not in mapping:
        for col in columns:
            if col not in used_columns and ("model" in col.lower() or "sku" in col.lower() or "id" in col.lower() or "code" in col.lower()):
                mapping["model_no"] = col
                used_columns.add(col)
                break
        if "model_no" not in mapping and columns:
            # Absolute fallback to next available column
            for col in columns:
                if col not in used_columns:
                    mapping["model_no"] = col
                    used_columns.add(col)
                    break

    return mapping


def generate_search_text(product_data: dict) -> str:
    """Generate search_text from ALL string fields for LLM retrieval.
    Includes product name, description, brand, model, specs — everything
    that could help the AI find this product in a customer query."""
    parts = []
    # Include all string values from the document (skip internal/meta fields)
    skip_keys = {"_id", "org_id", "source", "source_sheet_id", "search_text",
                 "unique_identifier", "created_at", "updated_at"}
    for key, val in product_data.items():
        if key in skip_keys:
            continue
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return " ".join(parts)


def auto_detect_product_type(columns: list[str]) -> str:
    """Auto-detect whether a sheet contains laptop or component data
    based on the presence of component-specific columns."""
    normalized = {col.strip().lower().replace(" ", "_") for col in columns}
    matches = normalized & COMPONENT_INDICATORS
    # If 2+ component-specific columns are present, classify as component
    return "component" if len(matches) >= 2 else "laptop"


def transform_row(
    row_data: dict,
    column_mapping: dict,
    sheet_id: str,
    org_id: str,
    product_type: str = "laptop",
) -> tuple[dict | None, str | None]:
    """Transform a sheet row dict into a product document matching the sheet columns 1:1 in snake_case.
    
    Args:
        row_data: Raw row from the sheet {column_name: value}
        column_mapping: Unused (kept for API compat) — mapping is 1:1 by column name
        sheet_id: Source sheet ID for provenance tracking
        org_id: Organization ID
        product_type: "laptop" or "component" — sets the `type` field on the doc
    
    Returns (product_doc, error_reason)."""
    doc = {}
    
    for col_name, value in row_data.items():
        if not value or not str(value).strip():
            continue
            
        clean_key = col_name.strip().lower().replace(" ", "_")
        
        val_str = str(value).strip().lower()
        if val_str == "yes" or val_str == "true":
            val = True
        elif val_str == "no" or val_str == "false":
            val = False
        else:
            try:
                if val_str.isdigit() or (val_str.startswith('-') and val_str[1:].isdigit()):
                    val = int(value)
                else:
                    val = float(value)
            except ValueError:
                val = str(value).strip()
                
        doc[clean_key] = val

    if not doc:
        return None, "empty row"

    # Find a unique identifier — try known unique key columns in priority order
    unique_key = None
    for k in ["sku", "sku_full", "model_no", "model_name", "id"]:
        if k in doc and str(doc[k]).strip():
            unique_key = str(doc[k]).strip()
            break
            
    if not unique_key:
        unique_key = str(list(doc.values())[0])

    doc["model_no"] = unique_key
    doc["unique_identifier"] = unique_key
    
    # Set the product type from the connection config
    doc["type"] = product_type
    
    # For components, set category from the sheet data or default
    if product_type == "component":
        if "category" not in doc or not doc.get("category"):
            doc["category"] = "component"
    elif "type" not in doc or not doc.get("type"):
        doc["type"] = product_type

    now = datetime.now(timezone.utc)
    doc["source"] = "google_sheet"
    doc["source_sheet_id"] = sheet_id
    doc["org_id"] = org_id
    doc["updated_at"] = now
    
    doc["search_text"] = generate_search_text(doc)

    return doc, None


# ─── Full Reconciliation Sync ──────────────────────────────────────────────────


async def sync_product_sheet(org_id: str, sheet_id: str) -> dict:
    """
    Full reconciliation sync: fetches ALL rows from Google Sheet, transforms
    using saved column mapping, upserts into products_{org_id}, and deletes
    products no longer in the sheet.

    Returns {products_added, products_updated, products_removed, products_skipped, errors}
    """
    db = get_mongo_db()

    # 1. Load connection config
    connection = await asyncio.to_thread(
        db.product_sheet_connections.find_one,
        {"org_id": org_id, "sheet_id": sheet_id}
    )
    if not connection:
        raise Exception(f"Product sheet connection not found for sheet {sheet_id}")

    column_mapping = connection["column_mapping"]
    tab_name = connection["tab_name"]
    product_type = connection.get("product_type", "laptop")

    # Mark as syncing
    await asyncio.to_thread(
        db.product_sheet_connections.update_one,
        {"org_id": org_id, "sheet_id": sheet_id},
        {"$set": {"status": "syncing", "updated_at": datetime.now(timezone.utc)}}
    )

    try:
        # 2. Refresh token
        access_token = await refresh_google_token(org_id)

        # 3. Detect header row
        headers, header_row, col_offset = await _detect_header_row(
            sheet_id, tab_name, access_token, org_id
        )
        if not headers:
            raise Exception("Could not detect headers in the sheet")

        # 4. Fetch ALL data rows
        encoded_tab = urllib.parse.quote(tab_name, safe="")
        data_start_row = header_row + 1
        range_str = f"{encoded_tab}!A{data_start_row}:ZZ10001"
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_str}"

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30
            )
            if resp.status_code == 401:
                access_token = await _force_token_refresh(org_id)
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30
                )
            if resp.status_code != 200:
                error_msg = resp.json().get("error", {}).get("message", "Unknown error")
                raise Exception(f"Google Sheets API error: {error_msg}")

        raw_rows = resp.json().get("values", [])

        # 5. Transform rows
        products_to_upsert = []
        errors = []
        current_model_nos = set()

        for row_idx, raw_row in enumerate(raw_rows):
            # Build row_data dict from headers
            row_data = {}
            adjusted_row = raw_row[col_offset:] if col_offset > 0 else raw_row
            for i, header in enumerate(headers):
                if i < len(adjusted_row):
                    row_data[header] = adjusted_row[i]
                else:
                    row_data[header] = ""

            product_doc, error = transform_row(row_data, column_mapping, sheet_id, org_id, product_type)
            if error:
                errors.append(f"Row {data_start_row + row_idx}: {error}")
                continue

            model_no = product_doc["model_no"]
            if model_no in current_model_nos:
                errors.append(f"Row {data_start_row + row_idx}: duplicate model_no '{model_no}'")
                continue

            current_model_nos.add(model_no)
            products_to_upsert.append(product_doc)

        # 6. Reconcile with MongoDB
        products_col = db[f"products_{org_id}"]

        # Ensure indexes
        await asyncio.to_thread(
            lambda: products_col.create_index(
                [("model_no", 1), ("source_sheet_id", 1)],
                unique=True,
                sparse=True,
                name="model_no_source_sheet_unique"
            )
        )
        await asyncio.to_thread(
            lambda: products_col.create_index(
                [("source_sheet_id", 1)],
                name="source_sheet_id_idx"
            )
        )

        now = datetime.now(timezone.utc)
        added = 0
        updated = 0

        # Batch upsert
        if products_to_upsert:
            operations = []
            for doc in products_to_upsert:
                operations.append(
                    UpdateOne(
                        {"model_no": doc["model_no"], "source_sheet_id": sheet_id},
                        {
                            "$set": doc,
                            "$setOnInsert": {"created_at": now}
                        },
                        upsert=True
                    )
                )

            # Execute in batches of 500
            for i in range(0, len(operations), 500):
                batch = operations[i:i+500]
                result = await asyncio.to_thread(
                    lambda b=batch: products_col.bulk_write(b)
                )
                added += result.upserted_count
                updated += result.modified_count

        # Delete stale products (from this sheet only)
        delete_filter = {
            "source_sheet_id": sheet_id,
        }
        if current_model_nos:
            delete_filter["model_no"] = {"$nin": list(current_model_nos)}
        else:
            # If no products in sheet, delete all products from this sheet
            pass

        delete_result = await asyncio.to_thread(
            lambda: products_col.delete_many(delete_filter)
        )
        removed = delete_result.deleted_count

        # 7. Update connection metadata
        total_products = await asyncio.to_thread(
            lambda: products_col.count_documents({"source_sheet_id": sheet_id})
        )

        await asyncio.to_thread(
            db.product_sheet_connections.update_one,
            {"org_id": org_id, "sheet_id": sheet_id},
            {"$set": {
                "status": "active",
                "last_synced_at": now,
                "last_sync_count": total_products,
                "sync_errors": errors[:20],  # Cap at 20 errors
                "updated_at": now,
            }}
        )

        # 8. Update org metadata
        total_all_products = await asyncio.to_thread(
            lambda: products_col.count_documents({})
        )
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
            f"Product sheet sync complete for org {org_id} sheet {sheet_id}: "
            f"{added} added, {updated} updated, {removed} removed, {len(errors)} errors"
        )

        return {
            "products_added": added,
            "products_updated": updated,
            "products_removed": removed,
            "products_skipped": len(errors),
            "errors": errors[:20],
        }

    except Exception as e:
        # Mark connection as error
        await asyncio.to_thread(
            db.product_sheet_connections.update_one,
            {"org_id": org_id, "sheet_id": sheet_id},
            {"$set": {"status": "error", "updated_at": datetime.now(timezone.utc)}}
        )
        logger.error(f"Product sheet sync failed for org {org_id} sheet {sheet_id}: {e}")
        raise
