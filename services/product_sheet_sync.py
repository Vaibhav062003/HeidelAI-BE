import asyncio
import httpx
import urllib.parse
from datetime import datetime, timezone

import re

from pymongo import UpdateOne
from pymongo.errors import BulkWriteError
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
    "graphic_card":                   ["graphic_card", "graphic card", "graphics_card", "graphics card", "gpu"],
    "graphic_card_space_gb":          ["graphic_card_space_gb", "graphic card space gb", "graphics_card_space_gb", "graphics card space gb", "gpu memory", "vram"],
    "ram_gb":                        ["ram_gb", "ram", "ram gb", "memory", "memory gb"],
    "storage_gb":                    ["storage_gb", "ssd", "storage", "storage gb", "ssd gb"],
    "ssd_type":                      ["ssd_type", "ssd type", "ssd_t", "sd_type", "storage type"],
    "hdd":                           ["hdd", "hard disk", "hard drive"],
    "smps":                          ["smps", "power supply", "psu"],
    "processor":                     ["processor", "cpu", "chip"],
    "processor_series":              ["processor_series", "processor series", "cpu series"],
    "motherboard_series":            ["motherboard_series", "motherboard series", "motherboard_d_series", "motherboard d series"],
    "motherboard_support":           ["motherboard_support", "motherboard support", "motherboard_processor_support", "motherboard processor support"],
    "cabinet":                       ["cabinet", "case", "chassis"],
    "category":                      ["category", "product category", "product type"],
    "in_stock":                      ["in_stock", "in stock"],
    "cpu_cooler":                    ["cpu_cooler", "cpu cooler"],
    "series":                        ["series", "product series"],
    "warranty":                      ["warranty", "warranty period", "guarantee"],
    # --- Case / Cabinet fields ---
    "odd_bays":                      ["odd_bays", "odd bays", "optical drive bays"],
    "hdd_bays":                      ["hdd_bays", "hdd bays", "hard drive bays"],
    "ssd_bays":                      ["ssd_bays", "ssd bays"],
    "rgb_support":                   ["rgb_support", "rgb support", "rgb"],
    "max_gpu_cooler_ht_mm":          ["max_gpu_cooler_ht_mm", "max gpu cooler ht mm", "max gpu cooler height", "gpu cooler height mm"],
    # --- Cooling fields ---
    "socket_type":                   ["socket_type", "socket type", "socket"],
    "air_cooler":                    ["air_cooler", "air cooler"],
    "liquid_cooler":                 ["liquid_cooler", "liquid cooler", "aio", "aio cooler"],
    "packaging_type":                ["packaging_type", "packaging type", "package type"],
    # --- Monitor fields ---
    "screen_size":                   ["screen_size", "screen size", "display size", "monitor size"],
    "panel_type":                    ["panel_type", "panel type", "display type"],
    "resolution":                    ["resolution", "display resolution", "native resolution"],
    "frame_rate":                    ["frame_rate", "frame rate", "refresh rate", "refresh_rate", "hz"],
    "flat_or_curved":                ["flat_or_curved", "flat or curved", "display shape", "curved"],
    "gaming_monitor":                ["gaming_monitor", "gaming monitor", "gaming"],
    "professional_monitor":          ["professional_monitor", "professional monitor", "professional"],
    "office_monitor":                ["office_monitor", "office monitor", "office"],
    "hdr":                           ["hdr", "hdr support", "hdr_support"],
    "adaptive_sync":                 ["adaptive_sync", "adaptive sync", "freesync", "gsync", "g-sync", "free_sync"],
    "smart":                         ["smart", "smart tv", "smart_tv"],
    "stand_type":                    ["stand_type", "stand type", "mount type"],
    "pivot_supported":               ["pivot_supported", "pivot supported", "pivot"],
    "pivot_range":                   ["pivot_range", "pivot range"],
    "response_time_ms":              ["response_time_ms", "response time ms", "response time", "response_time"],
    # --- Connectivity fields ---
    "wifi_supported":                ["wifi_supported", "wifi supported", "wifi", "wi-fi", "wi_fi"],
    "usb_c_supported":               ["usb_c_supported", "usb c supported", "usb-c", "usb_c", "type-c", "type_c"],
    "usb_c_details":                 ["usb_c_details", "usb c details", "usb-c details", "type-c details"],
    "hdmi":                          ["hdmi", "hdmi port", "hdmi_port", "hdmi ports"],
    "rear_color":                    ["rear_color", "rear color", "back color", "chassis color"],
}

CANONICAL_FIELDS = set(FIELD_ALIASES.keys())

# Columns that, if present, strongly indicate this is a component sheet
COMPONENT_INDICATORS = {
    "processor_series", "motherboard_series", "motherboard_support",
    "cabinet", "smps", "hdd", "ssd_type", "graphic_card", "ram",
    "processor", "gpu", "cpu", "psu", "cpu_cooler",
    "socket_type", "air_cooler", "liquid_cooler",
    "hdd_bays", "ssd_bays", "odd_bays",
    "gaming_monitor", "panel_type", "screen_size",
}

# ─── Field Name Normalization ──────────────────────────────────────────────────
# Auto-generated from FIELD_ALIASES: maps variant snake_case column names to
# the canonical DB field name so that "graphics_card" and "graphic_card" both
# resolve to the same key in the product document.

FIELD_NAME_NORMALIZE = {}
for _canonical, _aliases in FIELD_ALIASES.items():
    for _alias in _aliases:
        _normalized = re.sub(r'[^a-z0-9]+', '_', _alias.strip().lower()).strip('_')
        if _normalized != _canonical:
            FIELD_NAME_NORMALIZE[_normalized] = _canonical



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
    """Generate search_text from ALL fields for LLM retrieval.
    Includes product name, description, brand, model, specs — everything
    that could help the AI find this product in a customer query.
    Booleans are rendered as "field_name:yes/no" and numbers as "field_name:value"
    so the AI can search by spec (e.g. "16GB RAM", "wifi supported")."""
    parts = []
    # Include all values from the document (skip internal/meta fields)
    skip_keys = {"_id", "org_id", "source", "source_sheet_id", "source_sheet_ids",
                 "search_text", "unique_identifier", "created_at", "updated_at", "type"}
    for key, val in product_data.items():
        if key in skip_keys:
            continue
        if val is None:
            continue
        if isinstance(val, bool):
            parts.append(f"{key}:{'yes' if val else 'no'}")
        elif isinstance(val, (int, float)):
            parts.append(f"{key}:{val}")
        elif isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return " ".join(parts)


def auto_detect_product_type(columns: list[str]) -> str:
    """Auto-detect whether a sheet contains laptop or component data
    based on the presence of component-specific columns."""
    normalized = set()
    for col in columns:
        clean_key = col.strip().lower()
        clean_key = re.sub(r'[^a-z0-9]+', '_', clean_key).strip('_')
        if clean_key:
            clean_key = FIELD_NAME_NORMALIZE.get(clean_key, clean_key)
            normalized.add(clean_key)
            
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
        clean_key = col_name.strip().lower()
        clean_key = re.sub(r'[^a-z0-9]+', '_', clean_key).strip('_')
        if not clean_key:
            continue
        
        # Normalize variant field names to canonical DB field name
        clean_key = FIELD_NAME_NORMALIZE.get(clean_key, clean_key)
        
        # Handle empty/cleared cells explicitly by setting to None
        if value is None or str(value).strip() == "":
            doc[clean_key] = None
            continue
            
        val_str = str(value).strip().lower()
        if val_str == "yes" or val_str == "true":
            val = True
        elif val_str == "no" or val_str == "false":
            val = False
        else:
            try:
                if val_str.isdigit() or (val_str.startswith('-') and val_str[1:].isdigit()):
                    val = int(val_str)
                else:
                    val = float(val_str)
            except ValueError:
                val = str(value).strip()
                
        doc[clean_key] = val

    # Check if row is entirely empty (all values are None)
    if not any(v is not None for v in doc.values()):
        return None, "empty row"

    # Find a unique identifier — try known unique key columns in priority order
    unique_key = None
    for k in ["sku", "sku_full", "model_no", "model_name", "id"]:
        if doc.get(k) is not None and str(doc[k]).strip():
            unique_key = str(doc[k]).strip()
            break
            
    if not unique_key:
        # Fallback to the first non-empty value in the row
        for val in doc.values():
            if val is not None and str(val).strip():
                unique_key = str(val).strip()
                break

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
        products_col = db[f"products_{org_id}"]

        # ── Fix 3: One-time backfill of legacy products ──────────────────────
        # Only runs once per org; skipped on subsequent syncs via metadata flag
        org_meta = await asyncio.to_thread(
            db.organizations_metadata.find_one,
            {"org_id": org_id},
            {"_id": 0, "product_dedup_backfilled": 1}
        )
        if not (org_meta or {}).get("product_dedup_backfilled"):
            try:
                legacy_docs = await asyncio.to_thread(
                    lambda: list(products_col.find(
                        {"$or": [
                            {"unique_identifier": {"$in": [None, ""]}},
                            {"unique_identifier": {"$exists": False}},
                            {"type": {"$in": [None, ""]}},
                            {"type": {"$exists": False}},
                            {"source": {"$in": [None, ""]}},
                            {"source": {"$exists": False}},
                        ]},
                        {"_id": 1, "model_no": 1, "product_name": 1, "type": 1,
                         "unique_identifier": 1, "source_sheet_id": 1, "source": 1}
                    ))
                )
                if legacy_docs:
                    logger.info(f"[ProductSync] Backfilling {len(legacy_docs)} legacy products for org {org_id}")
                    backfill_ops = []
                    for doc in legacy_docs:
                        update_fields = {}
                        if not doc.get("unique_identifier"):
                            update_fields["unique_identifier"] = (
                                doc.get("model_no") or doc.get("product_name") or str(doc["_id"])
                            )
                        if not doc.get("type"):
                            update_fields["type"] = "laptop"
                        # Migrate legacy source_sheet_id → source_sheet_ids array
                        if doc.get("source_sheet_id"):
                            update_fields["source_sheet_ids"] = [doc["source_sheet_id"]]
                            if not doc.get("source"):
                                update_fields["source"] = "google_sheet"
                        elif not doc.get("source"):
                            update_fields["source"] = "manual"
                        if update_fields:
                            backfill_ops.append(
                                UpdateOne({"_id": doc["_id"]}, {"$set": update_fields})
                            )
                    if backfill_ops:
                        await asyncio.to_thread(
                            lambda ops=backfill_ops: products_col.bulk_write(ops, ordered=False)
                        )
                    logger.info(f"[ProductSync] Backfilled {len(backfill_ops)} legacy products")

                # Mark backfill as done so it never runs again for this org
                await asyncio.to_thread(
                    db.organizations_metadata.update_one,
                    {"org_id": org_id},
                    {"$set": {"product_dedup_backfilled": True}},
                    upsert=True
                )
            except Exception as e:
                logger.warning(f"[ProductSync] Legacy backfill failed (non-fatal): {e}")

        # ── Fix 2: Idempotent index creation (no drop) ───────────────────────
        try:
            await asyncio.to_thread(
                lambda: products_col.create_index(
                    [("type", 1), ("unique_identifier", 1)],
                    unique=True,
                    sparse=True,
                    name="type_unique_identifier_dedup"
                )
            )
        except Exception as idx_err:
            # If a legacy index with conflicting keys exists, drop it first (once)
            if "already exists with different options" in str(idx_err) or "IndexKeySpecsConflict" in str(idx_err):
                try:
                    existing_indexes = await asyncio.to_thread(lambda: list(products_col.list_indexes()))
                    for idx_info in existing_indexes:
                        idx_key = idx_info.get("key", {})
                        idx_name = idx_info.get("name", "")
                        # Drop legacy indexes that conflict
                        if idx_name in ("model_no_source_sheet_unique", "type_1_unique_identifier_1"):
                            await asyncio.to_thread(
                                lambda n=idx_name: products_col.drop_index(n)
                            )
                            logger.info(f"[ProductSync] Dropped conflicting legacy index: {idx_name}")
                    # Retry creation
                    await asyncio.to_thread(
                        lambda: products_col.create_index(
                            [("type", 1), ("unique_identifier", 1)],
                            unique=True,
                            sparse=True,
                            name="type_unique_identifier_dedup"
                        )
                    )
                except Exception as retry_err:
                    logger.error(f"[ProductSync] Failed to create dedup index after cleanup: {retry_err}")
            else:
                logger.warning(f"[ProductSync] Index creation warning: {idx_err}")

        await asyncio.to_thread(
            lambda: products_col.create_index(
                [("source_sheet_ids", 1)],
                name="source_sheet_ids_idx"
            )
        )

        # ── Refresh token & fetch data ───────────────────────────────────────
        access_token = await refresh_google_token(org_id)

        headers, header_row, col_offset = await _detect_header_row(
            sheet_id, tab_name, access_token, org_id
        )
        if not headers:
            raise Exception("Could not detect headers in the sheet")

        data_start_row = header_row + 1

        encoded_tab = urllib.parse.quote(tab_name, safe="")

        # ── Paginated fetch: pull rows in pages of 10,000 ─────────────────
        PAGE_SIZE = 10_000
        raw_rows = []
        page_start = data_start_row

        async with httpx.AsyncClient() as client:
            while True:
                page_end = page_start + PAGE_SIZE - 1
                range_str = f"{encoded_tab}!A{page_start}:ZZ{page_end}"
                url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_str}"

                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {access_token}"}, timeout=60
                )
                if resp.status_code == 401:
                    access_token = await _force_token_refresh(org_id)
                    resp = await client.get(
                        url, headers={"Authorization": f"Bearer {access_token}"}, timeout=60
                    )
                if resp.status_code != 200:
                    error_msg = resp.json().get("error", {}).get("message", "Unknown error")
                    raise Exception(f"Google Sheets API error: {error_msg}")

                page_rows = resp.json().get("values", [])
                if not page_rows:
                    break  # no more data

                raw_rows.extend(page_rows)
                logger.info(f"[ProductSync] Fetched page {page_start}-{page_end}: {len(page_rows)} rows (total so far: {len(raw_rows)})")

                if len(page_rows) < PAGE_SIZE:
                    break  # last page — fewer rows than requested

                page_start = page_end + 1

        # ── Transform rows ───────────────────────────────────────────────────
        products_to_upsert = []
        errors = []
        # Fix 4: Use unique_identifier for in-memory dedup (consistent with DB index)
        seen_unique_ids = set()

        for row_idx, raw_row in enumerate(raw_rows):
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

            uid = product_doc["unique_identifier"]
            if uid in seen_unique_ids:
                errors.append(f"Row {data_start_row + row_idx}: duplicate unique_identifier '{uid}'")
                continue

            seen_unique_ids.add(uid)
            products_to_upsert.append(product_doc)

        # ── Reconcile with MongoDB ───────────────────────────────────────────
        now = datetime.now(timezone.utc)
        added = 0
        updated = 0

        # Fix 1 + Fix 5: Upsert with source tracking and ordered=False
        if products_to_upsert:
            operations = []
            for doc in products_to_upsert:
                # Strip fields managed by $setOnInsert and $addToSet to avoid
                # MongoDB "Updating the path 'X' would create a conflict" errors
                set_doc = {k: v for k, v in doc.items()
                           if k not in ("source", "source_sheet_id", "source_sheet_ids", "created_at")}
                operations.append(
                    UpdateOne(
                        {"unique_identifier": doc["unique_identifier"], "type": doc["type"]},
                        {
                            # Update product data fields
                            "$set": set_doc,
                            # Track this sheet as a source (array, no duplicates)
                            "$addToSet": {"source_sheet_ids": sheet_id},
                            "$setOnInsert": {
                                "created_at": now,
                                "source": "google_sheet",
                            },
                        },
                        upsert=True
                    )
                )

            # Fix 5: Execute with ordered=False so all valid ops complete
            for i in range(0, len(operations), 500):
                batch = operations[i:i+500]
                try:
                    result = await asyncio.to_thread(
                        lambda b=batch: products_col.bulk_write(b, ordered=False)
                    )
                    added += result.upserted_count
                    updated += result.modified_count
                except BulkWriteError as bwe:
                    # Partial success — count what did succeed
                    details = bwe.details
                    added += details.get("nUpserted", 0)
                    updated += details.get("nModified", 0)
                    write_errors = details.get("writeErrors", [])
                    for we in write_errors[:5]:  # Log first 5 errors
                        errors.append(f"DB write error: {we.get('errmsg', 'unknown')}")
                    logger.warning(
                        f"[ProductSync] Batch {i//500+1} partial failure: "
                        f"{len(write_errors)} errors, {details.get('nUpserted',0)} upserted, "
                        f"{details.get('nModified',0)} modified"
                    )

        # ── Fix 1: Safe stale product deletion ───────────────────────────────
        # Only delete products that are EXCLUSIVELY owned by this sheet
        # (i.e. source_sheet_ids == [this_sheet] and no other sources)
        current_unique_ids = {doc["unique_identifier"] for doc in products_to_upsert}
        removed = 0

        if current_unique_ids:
            # Products from this sheet that are no longer in the sheet data
            delete_filter = {
                "source_sheet_ids": sheet_id,
                "org_id": org_id,
                "unique_identifier": {"$nin": list(current_unique_ids)},
            }
        else:
            # Sheet is empty — target all products from this sheet
            delete_filter = {
                "source_sheet_ids": sheet_id,
                "org_id": org_id,
            }

        # Find candidates for deletion
        stale_docs = await asyncio.to_thread(
            lambda: list(products_col.find(
                delete_filter,
                {"_id": 1, "source_sheet_ids": 1, "source": 1}
            ))
        )

        if stale_docs:
            delete_ids = []        # Products to fully remove
            unlink_ops = []        # Products to just remove this sheet from their source list

            for doc in stale_docs:
                sheet_ids = doc.get("source_sheet_ids", [])
                original_source = doc.get("source", "")

                if len(sheet_ids) <= 1 and original_source != "manual" and original_source != "csv_upload":
                    # Product only belongs to this sheet and wasn't manually uploaded — safe to delete
                    delete_ids.append(doc["_id"])
                else:
                    # Product has multiple sources or was manually uploaded — just unlink this sheet
                    unlink_ops.append(
                        UpdateOne(
                            {"_id": doc["_id"]},
                            {"$pull": {"source_sheet_ids": sheet_id}}
                        )
                    )

            if delete_ids:
                delete_result = await asyncio.to_thread(
                    lambda ids=delete_ids: products_col.delete_many({"_id": {"$in": ids}})
                )
                removed = delete_result.deleted_count

            if unlink_ops:
                await asyncio.to_thread(
                    lambda ops=unlink_ops: products_col.bulk_write(ops, ordered=False)
                )
                logger.info(f"[ProductSync] Unlinked {len(unlink_ops)} products from sheet {sheet_id} (kept in DB)")

        # ── Update connection metadata ───────────────────────────────────────
        total_products = await asyncio.to_thread(
            lambda: products_col.count_documents({"source_sheet_ids": sheet_id})
        )

        await asyncio.to_thread(
            db.product_sheet_connections.update_one,
            {"org_id": org_id, "sheet_id": sheet_id},
            {"$set": {
                "status": "active",
                "last_synced_at": now,
                "last_sync_count": total_products,
                "sync_errors": errors[:20],
                "updated_at": now,
            }}
        )

        # ── Update org metadata ──────────────────────────────────────────────
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

