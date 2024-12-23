
import os
from pinecone import Pinecone
from dotenv import load_dotenv
from phi.model.google import Gemini
from phi.agent import Agent, RunResponse
from sentence_transformers import SentenceTransformer

load_dotenv()

os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY")
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")

model = SentenceTransformer('all-MiniLM-L6-v2')

def initialize_pinecone():
    pc = Pinecone(api_key=PINECONE_API_KEY)

    index_name = "products-test"
    
    index = pc.Index(index_name)
    return index

index = initialize_pinecone()

def fetch_company_info(query: str) -> str:
    """
    Returns company information and policies. 
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

def fetch_product_data(query_str: str, category: str, price: str = "0") -> str:
    """
    Searches the vector database for products based on the user's query and returns their metadata.
    If price is not mentioned, use 0 as the default value.
    These are the only 3 categories: 
    "device" - laptops, phones, PS, etc.
    "custom" - custom built PCs
    "accessories" - keyboards, headphones, cooling pads, etc.
    Args:
        query_str (str): The user's search query, category, and price in the format "query, category, price".

    Returns:
        str: A confirmation of the products displayed to the user.
    """

    # Split the input string into components

    filters = {
        "category": category,
        "stock_quantity": {"$gt": 0}
    }
    if price != "0":
        filters["price"] = {"$lte": int(price)}

    print(f"Query: {query_str}, Category: {category}, Price: {price}")

    query_vector = model.encode(query_str).tolist()
    search_results = index.query(
        vector=query_vector,
        filter=filters,
        top_k=5,
        include_values=False,
        include_metadata=True
    )

    product_data = [
        result['metadata']
        for result in search_results['matches']  
    ]

    product_string = ""

    for product in product_data:
        product_string += f"Name: {product['name']}, Price: {product['price']}, Description: {product['description']}\n"

    return product_string

agent = Agent(
    model=Gemini(id="gemini-1.5-flash"),
    tools=[fetch_company_info, fetch_product_data],
    # Set add_history_to_messages=true to add the previous chat history to the messages sent to the Model.
    add_history_to_messages=True,
    # Number of historical responses to add to the messages.
    num_history_responses=3,
    description="You are a shopping assistant for Egenie4u. The company sells electronics and gadgets.",
    instructions=["Help user find the perfect product for their needs."],
    markdown=True,
    show_tool_calls=True,
)

while(True):

    user_input = input("You: ")

    run: RunResponse = agent.run(user_input)

    print(run.content)

    #For image inputs
    # agent.print_response(
    #     "The user wants the product in the image, identify the it and give a pharse to search it in the database.",
    #     images=["https://egeniestores.com/wp-content/uploads/2024/04/xps5-banner-desktop-new-1-1.jpg.pagespeed.ic.RD_KoqHfXI.webp"],
    #     stream=True,
    # )

