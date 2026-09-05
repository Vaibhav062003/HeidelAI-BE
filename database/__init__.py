import asyncio

from pinecone import Pinecone
from pymongo import MongoClient
from motor.motor_asyncio import AsyncIOMotorClient
from config.settings import MONGODB_URI, MONGODB_CLUSTER, PINECONE_API_KEY, PINECONE_INDEX_NAME

_client = None
_db = None

_pinecone_index = None

_async_client = None
_async_db = None

_probe_client = None
_mongo_available = False
MONGO_PROBE_TIMEOUT_MS = 3_000


def mark_mongo_unavailable() -> None:
    global _mongo_available
    _mongo_available = False


def mark_mongo_available() -> None:
    global _mongo_available
    _mongo_available = True


def mongo_retry_allowed() -> bool:
    return _mongo_available


async def wait_for_mongo_available() -> None:
    while not mongo_retry_allowed():
        await asyncio.sleep(1)


def probe_mongo() -> None:
    """Probe Atlas without changing normal application-client timeouts."""
    global _probe_client
    if _probe_client is None:
        _probe_client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=MONGO_PROBE_TIMEOUT_MS,
            connectTimeoutMS=MONGO_PROBE_TIMEOUT_MS,
            maxPoolSize=1,
        )
    _probe_client.admin.command("ping")


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


def close_mongo_clients() -> None:
    global _client, _db, _async_client, _async_db, _probe_client
    for client in (_client, _async_client, _probe_client):
        if client is not None:
            client.close()
    _client = _db = _async_client = _async_db = _probe_client = None
    mark_mongo_unavailable()
