import os
from pinecone import Pinecone
# from sentence_transformers import SentenceTransformer
from pymongo import MongoClient
import aiohttp
from fastapi import HTTPException
from typing import List
from config.settings import KOYEB2, MONGODB_URI

mongo_client = MongoClient(MONGODB_URI)
db = mongo_client['Cluster0']

async def get_query_vector(query: str) -> List[float]:
    """
    Call the encoding service API to get query vector

    Args:
        query (str): Query string to encode.

    Returns:
        List[float]: Encoded vector for the query.
    """
    url = f"{KOYEB2}/encode"  
    print("hitting donella")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"query": query}
            ) as response:
                if response:
                    data = await response.json()
                    # print("response", data["vector"])
                    return data["vector"]
                else:
                    print(f"Error from encoding service: {await response.text()}")
                    raise HTTPException(status_code=response.status, detail="Encoding service error")
    except Exception as e:
        print(f"Error calling encoding service: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def initialize_pinecone():
    """
    Initializes the Pinecone client and index.

    Returns:
        Index: Pinecone index object.
    """
    pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
    index = pc.Index("products-test")
    return index

index = initialize_pinecone()

async def search_pinecone(query: str, top_k: int = 10):
    """
    Searches Pinecone for the top-k matches for the given query.

    Args:
        index: Pinecone index object.
        query (str): Search query string.
        top_k (int): Number of top matches to return.

    Returns:
        list: List of top-k matched items with metadata.
    """
    # Convert query to embedding
    if query == "None":
        return []
    
    query_vector = await get_query_vector(query)

    # Query Pinecone
    results = index.query(
        vector=query_vector,
        top_k=top_k,
        include_metadata=True
    )

    return results['matches']

async def get_products(org_id: str, conversation_id: str):
    """
    Retrieves the query parameter from DB for the given conversation ID & searches this query on Pinecone.

    Args:
        conversation_id (str): The ID of the conversation.

    Returns:
        list: List of recommended products from Pinecone.
    """
    conversations_collection = db[f'conversations_{org_id}']
    conversation = conversations_collection.find_one({"id": conversation_id})
    search_phrase = conversation.get("query", None)
    if conversation and search_phrase:
        print("Found conversation for product_recommendations")
        print("Searching pinecone for ", search_phrase)
        search_results = await search_pinecone(search_phrase)
        products = []

        for result in search_results:
            product = result["metadata"]
            products.append(product)
            # print(product["name"])
        
        print("Total fetched recommended products: ", len(products))

        update_fields = {
            "product_recommendations": products,
        }

        # print("Updated fields: ", json.dumps(update_fields, indent=2))

        conversations_collection.update_one(
            {"id": conversation_id},
            {"$set": update_fields}
        )

        print(f"Updated recommended products for {conversation_id}.")
 
        return True
    print("No conversation found or no search phrase available.")
    return False
