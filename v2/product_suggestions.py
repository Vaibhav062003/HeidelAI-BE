from phi.agent import Agent, RunResponse
from pydantic import BaseModel
from phi.model.google import Gemini
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone
import os

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

class SearchQuery(BaseModel):
    query: str

def create_search_agent():
    """
    Creates a PhiData agent for generating a product search query.

    Returns:
        Agent: A configured PhiData agent.
    """
    agent = Agent(
        model=Gemini(id="gemini-1.5-flash"),
        description="You are a product search assistant.",
        instructions=[
            "Analyze the conversation and identify the latest mentioned product.",
            "Return only the search phrase for the product as a plain string.",
            "Ensure no extra explanations or output other than the search string."
        ],
        structured_output=SearchQuery,
    )
    return agent

def get_search_phrase(agent: Agent, conversation: str) -> str:
    """
    Uses the agent to generate a search phrase from the conversation.

    Args:
        agent (Agent): The PhiData agent.
        conversation (str): The conversation text.

    Returns:
        str: The search phrase for the latest mentioned product.
    """
    response: RunResponse = agent.run(conversation)
    return response.content.strip()

# Initialize SentenceTransformer model
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

def initialize_pinecone():
    """
    Initializes the Pinecone client and index.

    Returns:
        Index: Pinecone index object.
    """
    pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
    index = pc.Index("products-test")
    return index

def search_pinecone(index, query: str, top_k: int = 10):
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
    query_vector = embedding_model.encode(query).tolist()

    # Query Pinecone
    results = index.query(
        vector=query_vector,
        top_k=top_k,
        include_metadata=True
    )

    return results['matches']

search_agent = create_search_agent()
pinecone_index = initialize_pinecone()

conversation = """
Customer: laptops
AI: Sure! Are you looking for a specific brand or have any price range in mind?
Customer: I need a mid-range laptop under $1500 with good graphics performance.
AI: I recommend the TUF Gaming Laptop with RTX 3060, 16GB RAM, and a 512GB SSD. It's $1400.
Customer: That sounds great. Is it in stock, and what about the warranty?
AI: Yes, it is in stock, and it comes with a 1-year warranty.
Customer: Awesome, I'll think about it and get back to you.
"""
search_phrase = get_search_phrase(search_agent, conversation)
print("Search Phrase:", search_phrase)


results = search_pinecone(pinecone_index, search_phrase)
for match in results:
    print(f"Product: {match['metadata']['name']}, Score: {match['score']}")
