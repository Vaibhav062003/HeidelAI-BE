import asyncio
import hashlib
import httpx
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import re

from pymongo import ReturnDocument, UpdateOne
from pymongo.errors import BulkWriteError
from loguru import logger

from database import get_mongo_db
from api.google_sheets_routes import refresh_google_token, _force_token_refresh, _detect_header_row
from services.stock_gst import DEFAULT_GST_RATE, normalize_gst_inclusive, normalize_gst_rate
from services.pricing_policy import (
    normalize_currency,
    normalize_gst_inclusive as normalize_explicit_gst_inclusive,
    normalize_gst_rate as normalize_explicit_gst_rate,
    normalize_inr,
)

# ─── Field Aliases ─────────────────────────────────────────────────────────────
# Maps canonical MongoDB field names to possible sheet column name variations.
# Used by auto_suggest_mapping() for the preview endpoint and as the fallback
# normalization for columns not covered by a saved mapping.

FIELD_ALIASES = {
    # --- Shared / Laptop fields ---
    "product_name": ["product name", "product_name", "name", "title", "product title", "item name", "item"],
    "model_no":     ["model_no", "model no", "model number", "model", "sku", "item code", "product code", "part number", "part no", "article no"],
    "description":  ["description", "desc", "product description", "details", "short description"],
    # Keep price concepts separate. ``price`` remains a legacy display field;
    # it is never treated as an approved floor by the V1 pricing policy.
    "price":        ["price", "rate", "unit price", "amount"],
    "cost_price":   ["cost price", "purchase price", "landed cost", "landing cost", "dealer cost", "cost"],
    "approved_floor_price": [
        "approved floor price", "floor price", "minimum selling price",
        "min selling price", "approved minimum price",
    ],
    "target_selling_price": [
        "target selling price", "selling price", "target price", "offer price",
    ],
    "mrp": ["mrp", "maximum retail price", "maximum retail price mrp", "list price"],
    "currency": ["currency", "currency code"],
    "selling_gst_inclusive": [
        "gst inclusive", "selling gst inclusive", "selling_gst_inclusive",
        "tax inclusive", "price includes gst", "gst treatment",
    ],
    "cost_gst_inclusive": [
        "cost gst inclusive", "cost_gst_inclusive", "cost includes gst",
        "purchase price includes gst",
    ],
    # Retain exact legacy mapping keys saved before selling_gst_inclusive existed.
    "gst_inclusive": ["gst_inclusive"],
    "gst_rate":      ["gst rate", "gst_rate", "gst percent", "gst percentage", "gst %", "tax rate"],
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


def _field_name(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return FIELD_NAME_NORMALIZE.get(key, key)


def apply_saved_column_mapping(row_data: dict, column_mapping: dict) -> dict:
    """Apply the persisted ``{canonical_field: sheet_header}`` mapping.

    Unmapped columns are retained under their normalized names so catalogue
    search continues to receive arbitrary specification columns. A reverse
    mapping is accepted for compatibility with early preview clients.
    """
    headers_by_normalized = {_field_name(header): header for header in row_data}
    destinations: dict[str, str] = {}
    for raw_field, raw_header in (column_mapping or {}).items():
        field = _field_name(raw_field)
        header_key = _field_name(raw_header)
        if raw_header in row_data:
            destinations[str(raw_header)] = field
        elif header_key in headers_by_normalized:
            destinations[headers_by_normalized[header_key]] = field
        elif raw_field in row_data and header_key in CANONICAL_FIELDS:
            destinations[str(raw_field)] = header_key

    mapped = {}
    # First retain all unmapped fields for search/specification data.
    for header, value in row_data.items():
        if header not in destinations:
            mapped[_field_name(header)] = value
    # Apply explicit mappings last so an auto-normalized alias can never
    # override the merchant's saved choice for the same canonical field.
    for header, field in destinations.items():
        if header in row_data:
            mapped[field] = row_data[header]
    return mapped


_PRICE_FIELDS = (
    "cost_price", "approved_floor_price", "target_selling_price", "mrp",
)
_PRICE_PROVENANCE_FIELDS = (
    *_PRICE_FIELDS, "price", "currency", "selling_gst_inclusive",
    "cost_gst_inclusive", "gst_inclusive", "gst_rate",
    "price_source_sheet_id", "price_verified_at", "price_version",
)


def _price_version(sheet_id: str, doc: dict) -> str:
    values = "|".join(str(doc.get(field) if doc.get(field) is not None else "") for field in (
        *_PRICE_FIELDS, "currency", "selling_gst_inclusive", "cost_gst_inclusive", "gst_rate",
    ))
    identity = str(doc.get("unique_identifier") or doc.get("model_no") or "")
    return hashlib.sha256(f"{sheet_id}|{identity}|{values}".encode("utf-8")).hexdigest()


def _source_value_snapshot(doc: dict) -> dict:
    fields = (
        "product_name", "model_no", "sku", "description", "stock", "category",
        *_PRICE_PROVENANCE_FIELDS,
    )
    return {key: doc.get(key) for key in fields if key in doc}


def source_unlink_update(doc: dict, removed_sheet_id: str) -> dict:
    """Build an unlink update that cannot retain a removed price authority.

    Removing a non-authoritative source preserves the current price authority.
    Removing the authoritative source clears canonical approval and provenance;
    remaining source values may still provide a deterministic legacy display
    ``price``, but never become an approved authority implicitly.
    """
    remaining_ids = sorted(
        str(value) for value in (doc.get("source_sheet_ids") or [])
        if str(value) != str(removed_sheet_id)
    )
    source_values = doc.get("source_values") or {}
    current_source = str(doc.get("price_source_sheet_id") or "")
    preferred = current_source if current_source in remaining_ids else ""
    unset_fields = {
        f"source_rows.{removed_sheet_id}": "",
        f"source_values.{removed_sheet_id}": "",
    }
    if str(doc.get("source_sheet_id") or "") == str(removed_sheet_id):
        unset_fields["source_sheet_id"] = ""
    update = {
        "$pull": {"source_sheet_ids": removed_sheet_id},
        "$unset": unset_fields,
    }

    # Some legacy multi-source products predate source_values snapshots. If
    # the current authority is not the source being removed, preserve the
    # top-level verified fields instead of erasing evidence we cannot rebuild.
    preferred_snapshot = source_values.get(preferred) if preferred else None
    if preferred and not (
        isinstance(preferred_snapshot, dict)
        and any(key in preferred_snapshot for key in _PRICE_PROVENANCE_FIELDS)
    ):
        return update

    replacement_id = preferred or next(
        (sheet_id for sheet_id in remaining_ids if isinstance(source_values.get(sheet_id), dict)),
        "",
    )
    replacement = dict(source_values.get(replacement_id) or {})

    set_fields = {}
    if preferred:
        set_fields = {
            key: replacement[key]
            for key in _PRICE_PROVENANCE_FIELDS
            if key in replacement and replacement[key] is not None
        }
    for key in _PRICE_PROVENANCE_FIELDS:
        if key not in set_fields:
            unset_fields[key] = ""

    # A legacy source may supply display price, but it remains unverified.
    if replacement and "price" in replacement and replacement.get("price") is not None:
        set_fields["price"] = replacement["price"]
        unset_fields.pop("price", None)
    if (
        not preferred
        or not replacement.get("price_verified_at")
        or not replacement.get("price_version")
    ):
        for key in (*_PRICE_FIELDS, "price_source_sheet_id", "price_verified_at", "price_version"):
            set_fields.pop(key, None)
            unset_fields[key] = ""

    if set_fields:
        update["$set"] = set_fields
    return update


async def unlink_product_sheet_source(org_id: str, sheet_id: str) -> int:
    """Remove one Sheet source without leaving its price as authoritative."""
    database = get_mongo_db()
    products = database[f"products_{org_id}"]
    documents = await asyncio.to_thread(
        lambda: list(products.find(
            {"source_sheet_ids": sheet_id},
            {
                "_id": 1, "source": 1, "source_sheet_ids": 1, "source_values": 1,
                "source_sheet_id": 1, "price_source_sheet_id": 1,
            },
        ))
    )
    delete_ids = []
    updates = []
    for doc in documents:
        if (
            len(doc.get("source_sheet_ids") or []) <= 1
            and doc.get("source") not in {"manual", "csv_upload"}
        ):
            delete_ids.append(doc["_id"])
        else:
            updates.append(UpdateOne({"_id": doc["_id"]}, source_unlink_update(doc, sheet_id)))
    removed = 0
    if updates:
        await asyncio.to_thread(lambda: products.bulk_write(updates, ordered=False))
    if delete_ids:
        result = await asyncio.to_thread(
            lambda: products.delete_many({"_id": {"$in": delete_ids}})
        )
        removed += result.deleted_count
    legacy_documents = await asyncio.to_thread(
        lambda: list(products.find(
            {
                "source_sheet_id": sheet_id,
                "$or": [
                    {"source_sheet_ids": {"$exists": False}},
                    {"source_sheet_ids": {"$size": 0}},
                ],
            },
            {"_id": 1, "source": 1, "price_source_sheet_id": 1},
        ))
    )
    legacy_delete_ids = [
        doc["_id"] for doc in legacy_documents
        if doc.get("source") not in {"manual", "csv_upload"}
    ]
    legacy_updates = []
    for doc in legacy_documents:
        if doc["_id"] in legacy_delete_ids:
            continue
        unset = {"source_sheet_id": ""}
        if str(doc.get("price_source_sheet_id") or "") == str(sheet_id):
            for field in (*_PRICE_FIELDS, "price_source_sheet_id", "price_verified_at", "price_version"):
                unset[field] = ""
        legacy_updates.append(UpdateOne({"_id": doc["_id"]}, {"$unset": unset}))
    if legacy_updates:
        await asyncio.to_thread(lambda: products.bulk_write(legacy_updates, ordered=False))
    if legacy_delete_ids:
        result = await asyncio.to_thread(
            lambda: products.delete_many({"_id": {"$in": legacy_delete_ids}})
        )
        removed += result.deleted_count
    return removed


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
    """Transform a sheet row using the saved canonical-to-header mapping.
    
    Args:
        row_data: Raw row from the sheet {column_name: value}
        column_mapping: Persisted ``{canonical_field: sheet_header}`` mapping.
        sheet_id: Source sheet ID for provenance tracking
        org_id: Organization ID
        product_type: "laptop" or "component" — sets the `type` field on the doc
    
    Returns (product_doc, error_reason)."""
    doc = {}

    for clean_key, value in apply_saved_column_mapping(row_data, column_mapping).items():
        if not clean_key:
            continue

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
                numeric = (
                    re.sub(r"(?i)\b(?:inr|rs\.?)\b|₹|,|\s", "", val_str)
                    if clean_key in {"price", *_PRICE_FIELDS}
                    else val_str
                )
                if numeric.isdigit() or (numeric.startswith('-') and numeric[1:].isdigit()):
                    val = int(numeric)
                else:
                    val = float(numeric)
            except ValueError:
                val = str(value).strip()
        if clean_key in {"price", *_PRICE_FIELDS}:
            try:
                val = float(normalize_inr(value, field=clean_key))
            except ValueError as exc:
                return None, str(exc)
                
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

    try:
        selling_gst = normalize_explicit_gst_inclusive(
            doc.get("selling_gst_inclusive", doc.get("gst_inclusive")),
            field="selling_gst_inclusive",
        )
        cost_gst = normalize_explicit_gst_inclusive(
            doc.get("cost_gst_inclusive"), field="cost_gst_inclusive",
        )
        doc["currency"] = normalize_currency(doc.get("currency") or "INR")
        explicit_rate = doc.get("gst_rate")
        doc["gst_rate"] = float(normalize_explicit_gst_rate(
            DEFAULT_GST_RATE if explicit_rate is None else explicit_rate
        ))
    except ValueError as exc:
        return None, str(exc)
    doc["selling_gst_inclusive"] = selling_gst
    doc["cost_gst_inclusive"] = cost_gst
    # Legacy consumers still render ``price``/``gst_inclusive``. Cost and MRP
    # never flow into price; target then floor are the only canonical fallbacks.
    if doc.get("target_selling_price") is not None:
        doc["price"] = doc["target_selling_price"]
    elif doc.get("approved_floor_price") is not None:
        doc["price"] = doc["approved_floor_price"]
    doc["gst_inclusive"] = bool(selling_gst) if selling_gst is not None else False

    now = datetime.now(timezone.utc)
    doc["org_id"] = org_id
    doc["updated_at"] = now
    if any(doc.get(field) is not None for field in _PRICE_FIELDS):
        doc["price_source_sheet_id"] = sheet_id
        doc["price_verified_at"] = now
        doc["price_version"] = _price_version(sheet_id, doc)
    
    doc["search_text"] = generate_search_text(doc)

    return doc, None


# ─── Full Reconciliation Sync ──────────────────────────────────────────────────

PRODUCT_SHEET_SYNC_LEASE = timedelta(minutes=30)
PRODUCT_SHEET_RECONCILE_INTERVAL = timedelta(minutes=15)
PRODUCT_SHEET_RECONCILE_LEASE = timedelta(minutes=30)


async def _renew_product_sheet_sync_claim(db, org_id: str, sheet_id: str, claim_id: str) -> None:
    renewed = await asyncio.to_thread(
        db.product_sheet_connections.update_one,
        {
            "org_id": org_id,
            "sheet_id": sheet_id,
            "status": "syncing",
            "sync_claim_id": claim_id,
            "disconnect_requested_at": {"$exists": False},
        },
        {"$set": {
            "sync_claim_until": datetime.now(timezone.utc) + PRODUCT_SHEET_SYNC_LEASE,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    if not renewed.matched_count:
        raise Exception(f"Product sheet {sheet_id} disconnected during sync")


async def _release_product_sheet_sync_claim(db, org_id: str, sheet_id: str, claim_id: str) -> None:
    now = datetime.now(timezone.utc)
    disconnected = await asyncio.to_thread(
        db.product_sheet_connections.update_one,
        {
            "org_id": org_id,
            "sheet_id": sheet_id,
            "status": "syncing",
            "sync_claim_id": claim_id,
            "disconnect_requested_at": {"$exists": True},
        },
        {
            "$set": {"status": "disconnecting", "updated_at": now},
            "$unset": {"sync_claim_id": "", "sync_claim_until": ""},
        },
    )
    if not disconnected.matched_count:
        await asyncio.to_thread(
            db.product_sheet_connections.update_one,
            {
                "org_id": org_id,
                "sheet_id": sheet_id,
                "status": "syncing",
                "sync_claim_id": claim_id,
                "disconnect_requested_at": {"$exists": False},
            },
            {
                "$set": {"status": "error", "updated_at": now},
                "$unset": {"sync_claim_id": "", "sync_claim_until": ""},
            },
        )


async def begin_product_sheet_disconnect(
    org_id: str, sheet_id: str, *, wait_seconds: float = 30,
) -> None:
    """Fence an active sync before callers unlink any Sheet-owned products."""
    connections = get_mongo_db().product_sheet_connections
    now = datetime.now(timezone.utc)
    marked = await asyncio.to_thread(
        connections.update_one,
        {
            "org_id": org_id,
            "sheet_id": sheet_id,
            "status": {"$nin": ["syncing", "disconnecting"]},
        },
        {"$set": {"status": "disconnecting", "disconnect_requested_at": now}},
    )
    if marked.matched_count:
        return

    connection = await asyncio.to_thread(
        connections.find_one,
        {"org_id": org_id, "sheet_id": sheet_id},
        {"_id": 1, "status": 1, "sync_claim_id": 1, "sync_claim_until": 1},
    )
    if not connection:
        raise ValueError("Product sheet connection not found")
    if connection.get("status") == "disconnecting":
        return
    requested = await asyncio.to_thread(
        connections.update_one,
        {"_id": connection["_id"], "status": "syncing"},
        {"$set": {"disconnect_requested_at": now}},
    )
    if not requested.matched_count:
        return await begin_product_sheet_disconnect(org_id, sheet_id, wait_seconds=wait_seconds)

    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        current = await asyncio.to_thread(
            connections.find_one,
            {"_id": connection["_id"]},
            {"status": 1, "sync_claim_id": 1, "sync_claim_until": 1},
        )
        if not current:
            raise ValueError("Product sheet connection not found")
        if current.get("status") == "disconnecting":
            return
        lease_until = current.get("sync_claim_until")
        if isinstance(lease_until, datetime) and lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        if current.get("status") == "syncing" and isinstance(lease_until, datetime) and lease_until <= datetime.now(timezone.utc):
            expired = await asyncio.to_thread(
                connections.update_one,
                {
                    "_id": connection["_id"],
                    "status": "syncing",
                    "sync_claim_id": current.get("sync_claim_id"),
                    "disconnect_requested_at": {"$exists": True},
                },
                {
                    "$set": {"status": "disconnecting", "updated_at": datetime.now(timezone.utc)},
                    "$unset": {"sync_claim_id": "", "sync_claim_until": ""},
                },
            )
            if expired.matched_count:
                return
        if current.get("status") != "syncing":
            transitioned = await asyncio.to_thread(
                connections.update_one,
                {"_id": connection["_id"], "status": current.get("status")},
                {"$set": {"status": "disconnecting", "disconnect_requested_at": now}},
            )
            if transitioned.matched_count:
                return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Product sheet sync is still finishing; retry disconnect shortly")
        await asyncio.sleep(0.05)


async def sync_product_sheet(
    org_id: str, sheet_id: str, *, reconcile_claim_id: str | None = None,
) -> dict:
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
        {
            "org_id": org_id,
            "sheet_id": sheet_id,
            "status": {"$ne": "disconnecting"},
            "disconnect_requested_at": {"$exists": False},
        }
    )
    if not connection:
        raise Exception(f"Product sheet connection not found for sheet {sheet_id}")

    column_mapping = connection["column_mapping"]
    tab_name = connection["tab_name"]
    product_type = connection.get("product_type", "laptop")
    # Legacy single-sheet connections predate is_primary and are authoritative.
    is_primary_source = connection.get("is_primary") is not False

    # Claim the write phase. Scheduled recovery may replace only its own stale lease.
    sync_claim_id = uuid.uuid4().hex
    claim_filter = {
        "org_id": org_id,
        "sheet_id": sheet_id,
        "status": {"$in": ["active", "error"]},
        "disconnect_requested_at": {"$exists": False},
    }
    if reconcile_claim_id:
        claim_filter.update({
            "status": {"$in": ["active", "error", "syncing"]},
            "reconcile_lease_id": reconcile_claim_id,
        })
    marked = await asyncio.to_thread(
        db.product_sheet_connections.update_one,
        claim_filter,
        {"$set": {
            "status": "syncing",
            "sync_claim_id": sync_claim_id,
            "sync_claim_until": datetime.now(timezone.utc) + PRODUCT_SHEET_SYNC_LEASE,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    if not marked.matched_count:
        raise Exception(f"Product sheet {sheet_id} is already syncing or disconnecting")

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
                        await _renew_product_sheet_sync_claim(db, org_id, sheet_id, sync_claim_id)
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
                await _renew_product_sheet_sync_claim(db, org_id, sheet_id, sync_claim_id)
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
            product_doc["_sheet_row"] = data_start_row + row_idx
            products_to_upsert.append(product_doc)

        # ── Reconcile with MongoDB ───────────────────────────────────────────
        still_connected = await asyncio.to_thread(
            db.product_sheet_connections.find_one,
            {
                "org_id": org_id,
                "sheet_id": sheet_id,
                "status": "syncing",
                "sync_claim_id": sync_claim_id,
                "disconnect_requested_at": {"$exists": False},
            },
            {"_id": 1},
        )
        if not still_connected:
            raise Exception(f"Product sheet {sheet_id} disconnected during sync")
        now = datetime.now(timezone.utc)
        added = 0
        updated = 0

        # Fix 1 + Fix 5: Upsert with source tracking and ordered=False
        if products_to_upsert:
            operations = []
            for doc in products_to_upsert:
                sheet_row = doc.pop("_sheet_row")
                # Strip fields managed by $setOnInsert and $addToSet to avoid
                # MongoDB "Updating the path 'X' would create a conflict" errors
                set_doc = {k: v for k, v in doc.items()
                           if k not in ("source", "source_sheet_id", "source_sheet_ids", "created_at")}
                if not is_primary_source:
                    for field in _PRICE_PROVENANCE_FIELDS:
                        set_doc.pop(field, None)
                set_doc[f"source_rows.{sheet_id}"] = sheet_row
                set_doc[f"source_values.{sheet_id}"] = _source_value_snapshot(doc)
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
                    await _renew_product_sheet_sync_claim(db, org_id, sheet_id, sync_claim_id)
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
        await _renew_product_sheet_sync_claim(db, org_id, sheet_id, sync_claim_id)
        stale_docs = await asyncio.to_thread(
                lambda: list(products_col.find(
                    delete_filter,
                    {
                        "_id": 1, "source_sheet_ids": 1, "source": 1,
                        "source_values": 1, "source_sheet_id": 1,
                        "price_source_sheet_id": 1,
                    }
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
                            source_unlink_update(doc, sheet_id),
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

        await _renew_product_sheet_sync_claim(db, org_id, sheet_id, sync_claim_id)
        completed = await asyncio.to_thread(
            db.product_sheet_connections.update_one,
            {
                "org_id": org_id,
                "sheet_id": sheet_id,
                "status": "syncing",
                "sync_claim_id": sync_claim_id,
                "disconnect_requested_at": {"$exists": False},
            },
            {
                "$set": {
                    "status": "active",
                    "last_synced_at": now,
                    "last_sync_count": total_products,
                    "sync_errors": errors[:20],
                    "updated_at": now,
                },
                "$unset": {"sync_claim_id": "", "sync_claim_until": ""},
            },
        )
        if not completed.matched_count:
            await asyncio.to_thread(
                db.product_sheet_connections.update_one,
                {
                    "org_id": org_id,
                    "sheet_id": sheet_id,
                    "status": "syncing",
                    "sync_claim_id": sync_claim_id,
                    "disconnect_requested_at": {"$exists": True},
                },
                {
                    "$set": {"status": "disconnecting", "updated_at": now},
                    "$unset": {"sync_claim_id": "", "sync_claim_until": ""},
                },
            )
            raise Exception(f"Product sheet {sheet_id} disconnected during sync")

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

    except asyncio.CancelledError:
        await asyncio.shield(_release_product_sheet_sync_claim(db, org_id, sheet_id, sync_claim_id))
        raise
    except Exception as e:
        await _release_product_sheet_sync_claim(db, org_id, sheet_id, sync_claim_id)
        logger.error(f"Product sheet sync failed for org {org_id} sheet {sheet_id}: {e}")
        raise


async def reconcile_active_product_sheets(*, limit: int = 25) -> dict:
    """Lease and reconcile due product Sheets across all application replicas."""
    database = get_mongo_db()
    connections = database.product_sheet_connections
    try:
        await asyncio.to_thread(
            lambda: connections.create_index(
                [("status", 1), ("next_reconcile_at", 1), ("reconcile_lease_until", 1)],
                name="product_sheet_reconcile_due",
            )
        )
    except Exception as exc:
        logger.warning(f"[ProductSync] Reconciliation index unavailable: {exc}")

    claimed = succeeded = failed = 0
    errors: list[dict] = []
    for _ in range(max(0, min(int(limit), 100))):
        claim_now = datetime.now(timezone.utc)
        lease_id = uuid.uuid4().hex
        due_filter = {
            "$and": [
                {
                    "$or": [
                        {"next_reconcile_at": {"$lte": claim_now}},
                        {"next_reconcile_at": {"$exists": False}},
                    ]
                },
                {
                    "$or": [
                        {"status": {"$in": ["active", "error"]}},
                        {
                            "status": "syncing",
                            "sync_claim_until": {"$lt": claim_now},
                        },
                    ]
                },
                {
                    "$or": [
                        {"reconcile_lease_until": {"$lte": claim_now}},
                        {"reconcile_lease_until": {"$exists": False}},
                    ]
                },
            ]
        }
        connection = await asyncio.to_thread(
            lambda query=due_filter, claim=lease_id: connections.find_one_and_update(
                query,
                {
                    "$set": {
                        "reconcile_lease_id": claim,
                        "reconcile_lease_until": claim_now + PRODUCT_SHEET_RECONCILE_LEASE,
                        "next_reconcile_at": claim_now + PRODUCT_SHEET_RECONCILE_INTERVAL,
                    }
                },
                sort=[("next_reconcile_at", 1), ("updated_at", 1)],
                return_document=ReturnDocument.AFTER,
            )
        )
        if not connection:
            break
        claimed += 1
        org_id = str(connection.get("org_id") or "")
        sheet_id = str(connection.get("sheet_id") or "")
        try:
            if not org_id or not sheet_id:
                raise ValueError("Product Sheet connection is missing org_id or sheet_id")
            await sync_product_sheet(org_id, sheet_id, reconcile_claim_id=lease_id)
        except Exception as exc:
            failed += 1
            errors.append({"org_id": org_id, "sheet_id": sheet_id, "error": str(exc)[:300]})
            logger.error(f"[ProductSync] Scheduled reconciliation failed for org={org_id} sheet={sheet_id}: {exc}")
        else:
            succeeded += 1
        finally:
            completed_at = datetime.now(timezone.utc)
            await asyncio.to_thread(
                connections.update_one,
                {"_id": connection["_id"], "reconcile_lease_id": lease_id},
                {
                    "$unset": {"reconcile_lease_id": "", "reconcile_lease_until": ""},
                    "$set": {
                        "next_reconcile_at": completed_at + PRODUCT_SHEET_RECONCILE_INTERVAL,
                    },
                },
            )

    return {
        "claimed": claimed,
        "succeeded": succeeded,
        "failed": failed,
        "errors": errors,
    }


async def primary_product_sheet(org_id: str) -> dict:
    """Return the explicit write-back Sheet; a lone active Sheet becomes primary."""
    db = get_mongo_db()
    connections = await asyncio.to_thread(
        lambda: list(db.product_sheet_connections.find({
            "org_id": org_id, "status": {"$in": ["active", "syncing", "error"]},
            "disconnect_requested_at": {"$exists": False},
        }))
    )
    if not connections:
        raise ValueError("Connect a Google product Sheet before editing Sheet-backed stock")
    primary = [item for item in connections if item.get("is_primary")]
    if len(primary) == 1:
        if primary[0].get("status", "active") != "active":
            raise ValueError("Primary product Sheet is syncing or needs attention. Refresh stock and retry")
        return primary[0]
    if len(connections) != 1:
        raise ValueError("Choose one primary product Sheet before editing stock")
    if connections[0].get("status", "active") != "active":
        raise ValueError("Product Sheet is syncing or needs attention. Refresh stock and retry")
    await asyncio.to_thread(
        db.product_sheet_connections.update_one,
        {"_id": connections[0]["_id"]},
        {"$set": {"is_primary": True, "updated_at": datetime.now(timezone.utc)}},
    )
    connections[0]["is_primary"] = True
    return connections[0]


def _sheet_field(header: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")
    return FIELD_NAME_NORMALIZE.get(key, key)


def _column_letter(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet_value_equal(field: str, current, expected) -> bool:
    if expected is None:
        return str(current or "").strip() == ""
    if field in {"price", "stock", "gst_rate", *_PRICE_FIELDS}:
        try:
            clean = re.sub(r"(?i)\b(?:inr|rs\.?)\b|₹|,|\s", "", str(current))
            return abs(float(clean or 0) - float(expected)) <= 0.001
        except (TypeError, ValueError):
            return False
    if field == "gst_inclusive":
        return normalize_gst_inclusive(current) == normalize_gst_inclusive(expected)
    return str(current or "").strip() == str(expected or "").strip()


async def _ensure_gst_columns(
    org_id: str, sheet_id: str, tab_name: str, headers: list[str], header_row: int,
    access_token: str, fields: set[str], column_mapping: dict | None = None,
) -> tuple[list[str], dict[str, int], str]:
    by_field = _write_columns(headers, column_mapping or {})
    missing = [field for field in ("gst_inclusive", "gst_rate") if field in fields and field not in by_field]
    if not missing:
        return headers, by_field, access_token
    labels = {"gst_inclusive": "GST Inclusive", "gst_rate": "GST Rate"}
    encoded_tab = urllib.parse.quote("'" + tab_name.replace("'", "''") + "'", safe="")
    data = []
    for field in missing:
        index = len(headers)
        headers.append(labels[field])
        by_field[field] = index
        data.append({"range": f"{encoded_tab}!{_column_letter(index)}{header_row}", "values": [[labels[field]]]})
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchUpdate",
            json={"valueInputOption": "RAW", "data": data},
            headers={"Authorization": f"Bearer {access_token}"}, timeout=30,
        )
        if response.status_code == 401:
            access_token = await _force_token_refresh(org_id)
            response = await client.post(
                f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchUpdate",
                json={"valueInputOption": "RAW", "data": data},
                headers={"Authorization": f"Bearer {access_token}"}, timeout=30,
            )
        if response.status_code not in {200, 201}:
            raise ValueError(response.json().get("error", {}).get("message", "Google Sheet GST columns could not be created"))
    return headers, by_field, access_token


def _write_columns(headers: list[str], mapping: dict) -> dict[str, int]:
    nonempty = [header for header in headers if header.strip()]
    if len(set(nonempty)) != len(nonempty):
        raise ValueError("Duplicate Sheet headers must be resolved before writing stock")
    columns = apply_saved_column_mapping(
        {header: index for index, header in enumerate(headers) if header.strip()}, mapping,
    )
    # The stock UI's GST flag is the selling-price tax treatment.
    if "selling_gst_inclusive" in columns:
        columns["gst_inclusive"] = columns["selling_gst_inclusive"]
    return columns


async def write_product_sheet_values(org_id: str, product: dict, changes: dict) -> dict:
    """Serialize Sheet writes with reconciliation, then publish verified values."""
    connection = await primary_product_sheet(org_id)
    database = get_mongo_db()
    sheet_id = connection["sheet_id"]
    claim_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    claimed = await asyncio.to_thread(
        database.product_sheet_connections.update_one,
        {"org_id": org_id, "sheet_id": sheet_id, "status": "active",
         "disconnect_requested_at": {"$exists": False}},
        {"$set": {"status": "syncing", "sync_claim_id": claim_id,
                  "sync_claim_until": now + PRODUCT_SHEET_SYNC_LEASE,
                  "next_reconcile_at": now}},
    )
    if not claimed.matched_count:
        raise ValueError("Product Sheet is syncing or being edited. Refresh stock and retry")
    try:
        result = await _write_product_sheet_values(org_id, product, changes, connection)
        doc = result.pop("product_document")
        await _renew_product_sheet_sync_claim(database, org_id, sheet_id, claim_id)
        snapshot = _source_value_snapshot(doc)
        doc[f"source_values.{sheet_id}"] = snapshot
        doc[f"source_rows.{sheet_id}"] = result["row_number"]
        await asyncio.to_thread(
            database[f"products_{org_id}"].update_one,
            {"_id": product["_id"]} if product.get("_id") else {"unique_identifier": doc["unique_identifier"], "type": doc["type"]},
            {"$set": doc, "$addToSet": {"source_sheet_ids": sheet_id},
             "$setOnInsert": {"source": "google_sheet", "created_at": now}}, upsert=True,
        )
        return result
    finally:
        # A failed/ambiguous write is reconciled by the existing durable scheduler.
        async def release():
            for disconnecting in (True, False):
                await asyncio.to_thread(
                    database.product_sheet_connections.update_one,
                    {"org_id": org_id, "sheet_id": sheet_id, "sync_claim_id": claim_id,
                     "disconnect_requested_at": {"$exists": disconnecting}},
                    {"$set": {"status": "disconnecting" if disconnecting else "active",
                              "next_reconcile_at": datetime.now(timezone.utc)},
                     "$unset": {"sync_claim_id": "", "sync_claim_until": ""}},
                )
        await asyncio.shield(release())


async def _write_product_sheet_values(org_id: str, product: dict, changes: dict, connection: dict) -> dict:
    sheet_id = connection["sheet_id"]
    tab_name = connection.get("tab_name") or "Sheet1"
    access_token = await refresh_google_token(org_id)
    headers, header_row, col_offset = await _detect_header_row(sheet_id, tab_name, access_token, org_id)
    headers = [""] * col_offset + headers
    requested = {
        "price": changes.get("price"),
        "description": changes.get("description"),
        "stock": changes.get("quantity"),
        "gst_inclusive": changes.get("gst_inclusive"),
        "gst_rate": changes.get("gst_rate"),
    }
    requested = {key: value for key, value in requested.items() if value is not None}
    headers, by_field, access_token = await _ensure_gst_columns(
        org_id, sheet_id, tab_name, headers, header_row, access_token, set(requested),
        connection.get("column_mapping") or {},
    )
    # Match transform_row's display-price precedence. Never mutate an approved floor.
    if "price" in requested:
        if "target_selling_price" in by_field:
            requested["target_selling_price"] = requested.pop("price")
        elif "approved_floor_price" in by_field:
            raise ValueError("Map a Target Selling Price column before editing selling price; approved floor prices are not changed by Stock")
    missing = [key for key in requested if key not in by_field]
    if missing:
        raise ValueError(f"Primary Sheet is missing column(s): {', '.join(missing)}")

    encoded_tab = urllib.parse.quote("'" + tab_name.replace("'", "''") + "'", safe="")
    row_number = None
    async with httpx.AsyncClient() as client:
        if not row_number:
            sku_field = "model_no" if "model_no" in by_field else "sku" if "sku" in by_field else None
            if not sku_field:
                raise ValueError("Primary Sheet needs a Model No or SKU column before stock can be written")
            data_start_row = header_row + 1
            lookup_range = f"{encoded_tab}!A{data_start_row}:ZZ"
            lookup_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{lookup_range}"
            lookup = await client.get(
                lookup_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=60,
            )
            if lookup.status_code == 401:
                access_token = await _force_token_refresh(org_id)
                lookup = await client.get(
                    lookup_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=60,
                )
            if lookup.status_code != 200:
                raise ValueError("Google Sheet products could not be checked before writing")
            wanted = re.sub(r"[^a-z0-9]+", "", str(product.get("sku") or product.get("model_no") or product.get("name") or "").casefold())
            if not wanted:
                raise ValueError("A product SKU is required before writing stock")
            matches = []
            for offset, existing in enumerate(lookup.json().get("values") or []):
                index = by_field[sku_field]
                value = existing[index] if index < len(existing) else ""
                if re.sub(r"[^a-z0-9]+", "", str(value).casefold()) == wanted:
                    matches.append(data_start_row + offset)
            if len(matches) > 1:
                raise ValueError("Primary Sheet contains duplicate rows for this SKU")
            row_number = matches[0] if matches else None
            if not row_number and sheet_id in (product.get("source_sheet_ids") or []):
                raise ValueError("Product was removed or renamed in the primary Sheet. Sync stock and retry")
        if row_number:
            row_range = f"{encoded_tab}!A{row_number}:ZZ{row_number}"
            row_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{row_range}"
            response = await client.get(
                row_url,
                params={"valueRenderOption": "FORMULA"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
            if response.status_code == 401:
                access_token = await _force_token_refresh(org_id)
                response = await client.get(
                    row_url,
                    params={"valueRenderOption": "FORMULA"},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30,
                )
            if response.status_code != 200:
                raise ValueError("Google Sheet row could not be checked before writing")
            row = (response.json().get("values") or [[]])[0]
            sku_index = by_field[sku_field]
            actual = re.sub(r"[^a-z0-9]+", "", str(row[sku_index] if sku_index < len(row) else "").casefold())
            if actual != wanted:
                raise ValueError("Sheet row moved before the update; refresh stock and retry")
            protected = [
                headers[by_field[field]] for field in requested
                if by_field[field] < len(row) and str(row[by_field[field]]).startswith("=")
            ]
            if protected:
                raise ValueError(f"Formula-protected Sheet cell(s) cannot be overwritten: {', '.join(protected)}")
            baseline = (product.get("source_values") or {}).get(sheet_id) or product
            if product.get("source_sheet_ids") and any(
                not _sheet_value_equal(field, row[by_field[field]] if by_field[field] < len(row) else "", baseline.get(field))
                and not _sheet_value_equal(field, row[by_field[field]] if by_field[field] < len(row) else "", requested[field])
                for field in requested
            ):
                raise ValueError("Primary Sheet changed since the last sync. Refresh stock and retry")
            data = [{
                "range": f"{encoded_tab}!{_column_letter(by_field[field])}{row_number}",
                "values": [[value]],
            } for field, value in requested.items()]
            response = await client.post(
                f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchUpdate",
                json={"valueInputOption": "RAW", "data": data},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
            if response.status_code == 401:
                access_token = await _force_token_refresh(org_id)
                response = await client.post(
                    f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchUpdate",
                    json={"valueInputOption": "RAW", "data": data},
                    headers={"Authorization": f"Bearer {access_token}"}, timeout=30,
                )
        else:
            row = [""] * len(headers)
            row[by_field[sku_field]] = product.get("sku") or product.get("model_no") or product.get("name")
            for field, value in {
                "product_name": product.get("name") or product.get("product_name"),
                "description": product.get("description"), "price": product.get("price"),
                "stock": product.get("stock"), "category": product.get("category"),
            }.items():
                if field in by_field and value is not None:
                    row[by_field[field]] = value
            for field, value in requested.items():
                row[by_field[field]] = value
            if "product_name" in by_field and not row[by_field["product_name"]]:
                row[by_field["product_name"]] = product.get("name") or product.get("sku")
            response = await client.post(
                f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{encoded_tab}!A:ZZ:append",
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"values": [row]},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
            if response.status_code == 401:
                access_token = await _force_token_refresh(org_id)
                response = await client.post(
                    f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{encoded_tab}!A:ZZ:append",
                    params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                    json={"values": [row]}, headers={"Authorization": f"Bearer {access_token}"}, timeout=30,
                )
            match = re.search(r"![A-Z]+(\d+):", response.json().get("updates", {}).get("updatedRange", ""))
            row_number = int(match.group(1)) if match else None
        if response.status_code not in {200, 201}:
            raise ValueError("Google Sheet rejected the stock update; check Sheet permissions and retry")
        if not row_number:
            raise ValueError("Sheet write could not be verified; refresh stock before retrying")
        readback = await client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{encoded_tab}!A{row_number}:ZZ{row_number}",
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
            headers={"Authorization": f"Bearer {access_token}"}, timeout=30,
        )
        if readback.status_code == 401:
            access_token = await _force_token_refresh(org_id)
            readback = await client.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{encoded_tab}!A{row_number}:ZZ{row_number}",
                params={"valueRenderOption": "UNFORMATTED_VALUE"},
                headers={"Authorization": f"Bearer {access_token}"}, timeout=30,
            )
        if readback.status_code != 200:
            raise ValueError("Sheet was written but read-back failed; refresh stock before retrying")
        values = (readback.json().get("values") or [[]])[0]
        if any(not _sheet_value_equal(field, values[index] if index < len(values) else "", value)
               for field, value in requested.items() for index in [by_field[field]]):
            raise ValueError("Sheet values changed during the update; refresh stock and review")
        index = by_field[sku_field]
        actual = re.sub(r"[^a-z0-9]+", "", str(values[index] if index < len(values) else "").casefold())
        if actual != wanted:
            raise ValueError("Sheet row moved during the update; review the Sheet before retrying")
        document, error = transform_row(
            {header: values[index] if index < len(values) else "" for index, header in enumerate(headers) if header},
            connection.get("column_mapping") or {}, sheet_id, org_id, connection.get("product_type", "laptop"),
        )
        if error:
            raise ValueError(f"Sheet was written but catalogue validation failed: {error}")
    return {"sheet_id": sheet_id, "row_number": row_number, "updated_fields": sorted(requested), "product_document": document}


async def write_product_sheet_gst_bulk(
    org_id: str, products: list[dict], gst_inclusive: bool, gst_rate: float,
) -> dict:
    """Write one GST treatment to existing or new primary-Sheet product rows in batches."""
    connection = await primary_product_sheet(org_id)
    sheet_id = connection["sheet_id"]
    tab_name = connection.get("tab_name") or "Sheet1"
    access_token = await refresh_google_token(org_id)
    headers, header_row, col_offset = await _detect_header_row(sheet_id, tab_name, access_token, org_id)
    headers = [""] * col_offset + headers
    headers, by_field, access_token = await _ensure_gst_columns(
        org_id, sheet_id, tab_name, headers, header_row, access_token, {"gst_inclusive", "gst_rate"},
        connection.get("column_mapping") or {},
    )
    sku_field = "model_no" if "model_no" in by_field else "sku" if "sku" in by_field else None
    if not sku_field:
        raise ValueError("Primary Sheet needs a Model No or SKU column before GST can be written")

    encoded_tab = urllib.parse.quote(tab_name, safe="")
    data_start_row = header_row + 1
    lookup_range = f"{encoded_tab}!A{data_start_row}:ZZ"
    async with httpx.AsyncClient() as client:
        lookup = await client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{lookup_range}",
            params={"valueRenderOption": "FORMULA"},
            headers={"Authorization": f"Bearer {access_token}"}, timeout=60,
        )
        if lookup.status_code == 401:
            access_token = await _force_token_refresh(org_id)
            lookup = await client.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{lookup_range}",
                params={"valueRenderOption": "FORMULA"},
                headers={"Authorization": f"Bearer {access_token}"}, timeout=60,
            )
        if lookup.status_code != 200:
            raise ValueError("Google Sheet products could not be checked before applying GST")
        rows = lookup.json().get("values") or []
        by_sku: dict[str, list[int]] = {}
        for offset, row in enumerate(rows):
            value = row[by_field[sku_field]] if by_field[sku_field] < len(row) else ""
            key = re.sub(r"[^a-z0-9]+", "", str(value).casefold())
            if key:
                by_sku.setdefault(key, []).append(data_start_row + offset)

        updates, appends, skipped = [], [], []
        updated_rows = 0
        for product in products:
            sku = str(product.get("sku") or product.get("model_no") or product.get("name") or "").strip()
            key = re.sub(r"[^a-z0-9]+", "", sku.casefold())
            matches = by_sku.get(key, [])
            if len(matches) > 1:
                skipped.append({"sku": sku, "reason": "duplicate primary-Sheet rows"})
                continue
            if matches:
                row_number = matches[0]
                current = rows[row_number - data_start_row] if 0 <= row_number - data_start_row < len(rows) else []
                if any(
                    by_field[field] < len(current) and str(current[by_field[field]]).startswith("=")
                    for field in ("gst_inclusive", "gst_rate")
                ):
                    skipped.append({"sku": sku, "reason": "formula-protected GST cell"})
                    continue
                changed = False
                for field, value in (("gst_inclusive", gst_inclusive), ("gst_rate", gst_rate)):
                    current_value = current[by_field[field]] if by_field[field] < len(current) else ""
                    if not _sheet_value_equal(field, current_value, value):
                        updates.append({"range": f"{encoded_tab}!{_column_letter(by_field[field])}{row_number}", "values": [[value]]})
                        changed = True
                updated_rows += int(changed)
                continue
            row = [""] * len(headers)
            row[by_field[sku_field]] = sku
            for field, value in {
                "product_name": product.get("name") or product.get("product_name") or sku,
                "description": product.get("description"),
                "price": product.get("price"),
                "stock": product.get("quantity", product.get("stock")),
                "category": product.get("category"),
                "gst_inclusive": gst_inclusive,
                "gst_rate": gst_rate,
            }.items():
                if field in by_field and value is not None:
                    row[by_field[field]] = value
            appends.append(row)

        for start in range(0, len(updates), 500):
            response = await client.post(
                f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchUpdate",
                json={"valueInputOption": "RAW", "data": updates[start:start + 500]},
                headers={"Authorization": f"Bearer {access_token}"}, timeout=60,
            )
            if response.status_code not in {200, 201}:
                raise ValueError(response.json().get("error", {}).get("message", "Google Sheet rejected the bulk GST update"))
        for start in range(0, len(appends), 500):
            response = await client.post(
                f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{encoded_tab}!A:ZZ:append",
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"values": appends[start:start + 500]},
                headers={"Authorization": f"Bearer {access_token}"}, timeout=60,
            )
            if response.status_code not in {200, 201}:
                raise ValueError(response.json().get("error", {}).get("message", "Google Sheet rejected new GST product rows"))
    await sync_product_sheet(org_id, sheet_id)
    return {
        "sheet_id": sheet_id, "updated": updated_rows, "appended": len(appends),
        "skipped": skipped, "gst_inclusive": gst_inclusive, "gst_rate": gst_rate,
    }
