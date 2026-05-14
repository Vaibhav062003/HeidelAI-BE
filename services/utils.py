from zoneinfo import ZoneInfo

from bson import ObjectId
from datetime import datetime, timezone
import os
import base64
import time

def serialize_mongo(obj):
    if isinstance(obj, ObjectId):
        return str(obj)

    if isinstance(obj, datetime):
        # Ensure timezone info is always present so JS parses as UTC
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        else:
            obj = obj.astimezone(timezone.utc)
        return obj.isoformat().replace('+00:00', 'Z')

    if isinstance(obj, list):
        return [serialize_mongo(i) for i in obj]

    if isinstance(obj, dict):
        serialized = {}

        for key, value in obj.items():
            if key == "_id":
                serialized["id"] = serialize_mongo(value)
            else:
                serialized[key] = serialize_mongo(value)

        return serialized

    return obj

def generate_message_id():
    timestamp = int(time.time() * 1000)          # milliseconds
    random_bytes = os.urandom(32)                # 256-bit randomness
    raw = f"{timestamp}:".encode() + random_bytes
    message_id = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return message_id

def inject_variables(data, variables: dict):
    """
    Recursively searches through lists and dictionaries to replace 
    placeholder strings like {customer_name} with actual values.
    """
    if isinstance(data, dict):
        return {key: inject_variables(value, variables) for key, value in data.items()}
    elif isinstance(data, list):
        return [inject_variables(item, variables) for item in data]
    elif isinstance(data, str):
        # Replace all matching placeholders in the string
        for key, val in variables.items():
            # Using .replace() handles multiple occurrences in the same string
            data = data.replace(f"{{{key}}}", "" if val is None else str(val))
        return data
    
    # Return ints, bools, etc., as is
    return data

def normalize_phone(phone_raw: str, default_country_code: str = "91") -> str:
    """
    Robust normalization to Meta's format (no symbols, includes country code).
    Ensures that numbers with/without prefixes map to the same canonical ID.
    
    Example (India):
    - '9133800607'   -> '919133800607'
    - '919133800607' -> '919133800607'
    - '+91 91338-00607' -> '919133800607'
    """
    if not phone_raw:
        return ""
    
    import re
    # Strip all non-digits
    clean = re.sub(r"[^\d]", "", str(phone_raw))
    
    # Strip leading 0s (common in local formats)
    clean = clean.lstrip("0")
    
    if not clean:
        return ""

    # Special logic for India (default_country_code="91")
    # If it's a 10-digit number, prepend the country code.
    if len(clean) == 10:
        return f"{default_country_code}{clean}"
        
    # If it's already a 12-digit number starting with 91, it's likely already normalized.
    # However, if someone provided an 8-digit or other length, we return the cleaned digits.
    
    return clean

def get_first_name(full_name: str):
    if not full_name:
        return "there"

    parts = full_name.strip().split()
    return parts[0] if parts else "there"

def format_in_ist(record):
    # Convert Loguru's default aware datetime to IST
    ist_time = record["time"].astimezone(ZoneInfo("Asia/Kolkata"))
    
    # Format the time: %I is 12-hour clock, %M is minute, %S is second, %p is AM/PM
    record["extra"]["formatted_time"] = ist_time.strftime("%d-%m-%y %I:%M:%S %p")
    
    # Return the log string format (matching Loguru's default color style)
    return "<green>{extra[formatted_time]}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>\n"