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
