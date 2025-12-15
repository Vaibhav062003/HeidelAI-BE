from bson import ObjectId
from datetime import datetime

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