import os
from pinecone import Pinecone
from dotenv import load_dotenv
from pymongo import MongoClient
from config.settings import MONGODB_URI, MONGODB_CLUSTER, PINECONE_API_KEY, PINECONE_INDEX_NAME

load_dotenv()

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
        _db = get_client()[MONGODB_CLUSTER]
    return _db

def get_pinecone_db():
    pc = Pinecone(api_key=PINECONE_API_KEY)

    index_name = PINECONE_INDEX_NAME

    index = pc.Index(index_name)
    return index