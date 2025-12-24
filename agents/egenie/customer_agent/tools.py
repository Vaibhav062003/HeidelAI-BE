from typing import List
from database import get_pinecone_db
from langchain_core.tools import tool
from .models import ProductResponse, Product
from fastapi import HTTPException
import aiohttp
# from sentence_transformers import SentenceTransformer
# from config.settings import SENTENCE_TRANSFORMERS_MODEL

from config.settings import KOYEB2
from core.logger import get_logger

logger = get_logger(__name__)

index = get_pinecone_db()
# model = SentenceTransformer(SENTENCE_TRANSFORMERS_MODEL)

@tool
def fetch_company_info(dummy: str) -> str:
    """
    Fetches both company information and policies for Egenie4u.

    Returns:
        str: A confirmation of the products displayed to the user.
    """
    return """
    Egenie4u Policies:
    - Easy returns and fast shipping.
    - All products come with GST bills and are brand new, sealed.
    - Warranty claims supported through respective companies using Egenie4u's GST bill.

    About Egenie4u:
    - 22 years of wholesale expertise translated into competitive retail pricing.
    - Wide range of products, including high-end gaming PCs, consoles, and computer peripherals.
    - Regular promotions and discounts on popular items.
    - Commitment to customer satisfaction and after-sales support.
    """

async def get_query_vector(query: str) -> List[float]:
    """
    Call the encoding service API to get query vector

    Args:
        query (str): Query string to encode.

    Returns:
        List[float]: Encoded vector for the query.
    """
    url = f"{KOYEB2}/encode"  
    logger.info("hitting donella")
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
                    logger.error(f"Error from encoding service: {await response.text()}")
                    raise HTTPException(status_code=response.status, detail="Encoding service error")
    except Exception as e:
        logger.error(f"Error calling encoding service: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@tool
def product_search_tool(query: str) -> ProductResponse:
    """
    This tool retrieves products based on the user's query.
    The products are retrieved from the Pinecone database.

    Args:
        query (str): The search query provided by the user.

    Returns:
        ProductResponse: A response object containing the search results.
    """
    # query_vector = model.encode(query).tolist()
    query_vector = get_query_vector(query)
    search_results = index.query(
        vector=query_vector,
        top_k=5,
        include_values=False,
        include_metadata=True
    )

    return [Product(**result['metadata']) for result in search_results['matches']]

tools = [
    product_search_tool,
    # fetch_company_info
]

if __name__ == "__main__":
    pass