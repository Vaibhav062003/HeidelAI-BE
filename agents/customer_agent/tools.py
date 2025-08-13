from typing import List
from database import get_pinecone_db
from langchain_core.tools import tool
from .models import ProductResponse, Product
from sentence_transformers import SentenceTransformer
from config.settings import SENTENCE_TRANSFORMERS_MODEL

index = get_pinecone_db()
model = SentenceTransformer(SENTENCE_TRANSFORMERS_MODEL)

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
    query_vector = model.encode(query).tolist()
    search_results = index.query(
        vector=query_vector,
        top_k=5,
        include_values=False,
        include_metadata=True
    )

    return [Product(**result['metadata']) for result in search_results['matches']]

tools = [
    product_search_tool,
    fetch_company_info
]

if __name__ == "__main__":
    pass