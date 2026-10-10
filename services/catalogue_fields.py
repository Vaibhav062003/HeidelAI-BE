"""Stable IDs for both migrated and legacy catalogue documents."""
from bson import ObjectId
import math
import re

_WORDS = re.compile(r"[^a-z0-9.]+")


def _code_tokens(values) -> set[str]:
    found = {str(value).strip().casefold() for value in values if value}
    for value in values:
        if not value:
            continue
        text = str(value)
        for part in re.split(r"[^A-Za-z0-9]+", text):
            low = part.casefold()
            if len(low) >= 5 and any(c.isalpha() for c in low) and any(c.isdigit() for c in low):
                found.add(low)
        compact = re.sub(r"[^A-Za-z0-9]+", "", text).casefold()
        if 5 <= len(compact) <= 40 and any(c.isalpha() for c in compact) and any(c.isdigit() for c in compact):
            found.add(compact)
    return found


def product_id(document: dict) -> str:
    if document.get("product_id"):
        return str(document["product_id"])
    identity = document.get("_id")
    if identity is None:
        return ""
    kind = "oid" if isinstance(identity, ObjectId) else "str"
    return f"mongo:{kind}:{identity}"


def lookup(identity: str, org_id: str) -> dict:
    if not identity.startswith(("mongo:oid:", "mongo:str:")):
        return {"org_id": org_id, "product_id": identity}
    alternatives = [{"product_id": identity}]
    if identity.startswith("mongo:oid:") and ObjectId.is_valid(identity[10:]):
        alternatives.append({"_id": ObjectId(identity[10:])})
    elif identity.startswith("mongo:str:"):
        alternatives.append({"_id": identity[10:]})
    return {"org_id": org_id, "$or": alternatives}


def canonical_fields(document: dict) -> dict:
    """Additive ingestion fields; never choose identity from an arbitrary cell."""
    raw_codes = document.get("search_codes") or []
    aliases = list(raw_codes) if isinstance(raw_codes, list) else [str(raw_codes)]
    identifiers = []
    for key in ("sku", "sku_full", "sku_prefix", "sku_suffix", "model_no",
                "unique_identifier", "product_id"):
        if document.get(key) and not (key == "product_id" and str(document[key]).startswith("mongo:")):
            identifiers.append(document[key])
            aliases.append(str(document[key]).strip().casefold())
    aliases.extend(_code_tokens([*identifiers, document.get("model_name")]))
    result = {"product_id": product_id(document),
            "brand_key": str(document.get("brand") or "").strip().casefold(),
            "search_codes": sorted({str(code).strip().casefold() for code in aliases if code})}
    category = str(document.get("category") or document.get("type") or "").strip().casefold()
    if category:
        result["category"] = re.sub(r"\W+", "_", category).strip("_")
    if not document.get("model_name") and document.get("product_name"):
        result["model_name"] = document["product_name"]
    specs = dict(document.get("specs") or {})
    for key, source_names in {
        "processor": ("processor", "cpu"), "graphics_card": ("graphics_card", "gpu", "graphics", "graphic_card"),
        "graphic_card": ("graphic_card",),
        "ram_gb": ("ram_gb", "ram", "memory"), "storage_gb": ("storage_gb", "storage", "ssd"),
        "ram": ("ram", "memory"), "storage": ("storage", "ssd"),
        "graphic_card_space_gb": ("graphic_card_space_gb", "vram_gb", "vram"),
        "display_hz": ("display_hz", "frame_rate", "refresh_rate", "hz"),
        "screen_size": ("screen_size", "display_inches"),
        "display_inches": ("display_inches", "screen_size"),
        "display_type": ("display_type", "panel_type"),
        "panel_type": ("panel_type", "display_type"),
        "operating_system": ("operating_system", "os"),
        "processor_brand": ("processor_brand",), "processor_family": ("processor_family",),
        "processor_generation": ("processor_generation",),
        "processor_model_no": ("processor_model_no",), "is_touch": ("is_touch",),
        "processor_series": ("processor_series",), "processor_suffix": ("processor_suffix",),
        "type": ("type", "category", "category_label"),
    }.items():
        value = next((document[name] for name in source_names if document.get(name) is not None), specs.get(key))
        if value is None:
            continue
        if key.endswith("_gb"):
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(gb|tb)?\s*", str(value), re.I)
            if not match:
                continue
            value = float(match.group(1)) * (1024 if (match.group(2) or "").lower() == "tb" else 1)
            if not math.isfinite(value) or value <= 0:
                continue
        elif key in ("display_inches", "screen_size"):
            try:
                value = float(str(value).replace('"', '').strip())
            except ValueError:
                pass
        elif key in ("processor_model_no", "processor_series", "display_hz", "graphic_card_space_gb"):
            try:
                value = int(float(str(value).lower().replace("hz", "").replace("gb", "").replace("vram", "").strip()))
            except ValueError:
                pass
        elif key == "is_touch":
            if isinstance(value, str):
                value = str(value).strip().casefold() in ("true", "yes", "1")
            else:
                value = bool(value)
        elif isinstance(value, str):
            value = re.sub(r"_+", " ", value).strip()
            
        specs[key] = value
        result[key] = value
    if specs:
        result["specs"] = specs
    if (any(document.get(key) is not None for key in ("stock", "quantity", "available_qty", "in_stock", "stock_available"))
            or document.get("stock_state") not in {"listed", "out_of_stock", "unknown"}):
        stock = next((document[key] for key in ("stock", "quantity", "available_qty", "in_stock", "stock_available") if document.get(key) is not None), None)
        state = "unknown"
        if stock is not None:
            text = str(stock).strip().casefold()
            if text in {"true", "yes", "available", "in stock"}:
                state = "listed"
            elif text in {"false", "no", "unavailable", "out of stock"}:
                state = "out_of_stock"
            else:
                try:
                    amount = float(stock)
                    if math.isfinite(amount) and amount >= 0:
                        state = "listed" if amount > 0 else "out_of_stock"
                except (ValueError, TypeError):
                    pass
        result["stock_state"] = state
        result["stock_available"] = (state == "listed")
    merged = {**document, **result}
    text_values = [merged.get("category"), merged.get("brand"), merged.get("model_name"),
                   *result["search_codes"], *(merged.get("specs") or {}).values()]
    result["search_text"] = " ".join(dict.fromkeys(
        word for value in text_values for word in _WORDS.split(str(value or "").casefold()) if word
    ))
    head = " ".join(filter(None, (str(merged.get("brand") or "").strip(),
                                   str(merged.get("model_name") or "").strip())))
    category = str(merged.get("category") or "product").replace("_", " ")
    facts = ", ".join(f"{key.replace('_', ' ')} {value}"
                      for key, value in (merged.get("specs") or {}).items())
    result["semantic_text"] = f"{head}, a {category}" + (f" with {facts}." if facts else ".")
    return result
