from zoneinfo import ZoneInfo

from bson import ObjectId
from datetime import datetime
import os
import base64
import time

def serialize_mongo(obj):
    if isinstance(obj, ObjectId):
        return str(obj)

    if isinstance(obj, datetime):
        return obj.isoformat()

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