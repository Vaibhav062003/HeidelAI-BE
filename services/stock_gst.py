import math
import re
from typing import Any


DEFAULT_GST_RATE = 18.0


def normalize_gst_inclusive(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "yes", "true", "included", "inclusive", "gst included"}


def normalize_gst_rate(value: Any, default: float = DEFAULT_GST_RATE) -> float:
    if isinstance(value, bool):
        return default
    try:
        rate = float(re.sub(r"[%\s,]", "", str(value)))
    except (TypeError, ValueError):
        return default
    return rate if math.isfinite(rate) and 0 <= rate <= 100 else default


def normalize_category(value: Any) -> str:
    return str(value or "").strip() or "Uncategorised"


def select_gst_rows(rows: list[dict], scope: str, category: str = "") -> list[dict]:
    if scope == "all":
        return rows
    if scope != "category":
        raise ValueError("GST scope must be all or category")
    wanted = normalize_category(category).casefold()
    return [row for row in rows if normalize_category(row.get("category")).casefold() == wanted]
