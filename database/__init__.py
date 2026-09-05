from pinecone import Pinecone
from pymongo import MongoClient
from motor.motor_asyncio import AsyncIOMotorClient
from config.settings import MONGODB_URI, MONGODB_CLUSTER, PINECONE_API_KEY, PINECONE_INDEX_NAME

_client = None
_db = None

_pinecone_index = None

_async_client = None
_async_db = None

def get_async_mongo_client():
    global _async_client
    if _async_client is None:
        _async_client = AsyncIOMotorClient(MONGODB_URI)
    return _async_client

def get_async_mongo_db():
    global _async_db
    if _async_db is None:
        client = get_async_mongo_client()
        _async_db = client[MONGODB_CLUSTER]
    return _async_db

def get_mongo_client():
    global _client
    if _client is None:
        _client = MongoClient(MONGODB_URI)
    return _client

def get_mongo_db():
    global _db
    if _db is None:
        _db = get_mongo_client()[MONGODB_CLUSTER]
    return _db

def get_pinecone_db():
    global _pinecone_index
    if _pinecone_index is None:
        pc = Pinecone(api_key=PINECONE_API_KEY)
        _pinecone_index = pc.Index(PINECONE_INDEX_NAME)
    return _pinecone_index