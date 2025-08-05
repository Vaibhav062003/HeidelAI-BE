import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_CLUSTER", "Cluster0")

_client = None
_db = None

def get_client():
    global _client
    if _client is None:
        _client = MongoClient(MONGODB_URI)
    return _client

def get_mongo_db():
    global _db
    if _db is None:
        _db = get_client()[MONGODB_DB]
    return _db
