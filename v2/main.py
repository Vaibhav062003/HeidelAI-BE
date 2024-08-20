import os, time
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.conversation.memory import ConversationBufferWindowMemory
from langchain.agents import Tool
from langchain.agents import initialize_agent

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LANGCHAIN_TRACING_V2 = os.getenv("LANGCHAIN_TRACING_V2")
LANGCHAIN_ENDPOINT = os.getenv("LANGCHAIN_ENDPOINT")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
LANGCHAIN_PROJECT = os.getenv("LANGCHAIN_PROJECT")


model = SentenceTransformer('all-MiniLM-L6-v2')

def initialize_pinecone():
    pc = Pinecone(api_key="84641b3f-7be7-4a15-aa8e-91552da0ca04")

    index_name = "egenie-test"
    
    index = pc.Index(index_name)
    return index


def initialize_first_llm():
    llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)

    return llm

def fetch_company_info(query: str) -> str:
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

company_info_policies_tool = Tool(
    name="fetch_company_info",
    func=fetch_company_info,
    description="Fetches both company information and policies for Egenie4u."
)

def fetch_product_data(query_str: str) -> list:
    """
    Searches the vector database for products based on the user's query and returns their metadata.

    Args:
        query_str (str): The user's search query, category, and price in the format "query, category, price".

    Returns:
        list: A list of metadata of products that match the search query.
    """
    # Split the input string into components
    parts = query_str.split(",")
    if len(parts) < 2:
        raise ValueError("Input string must include at least query and category.")
    
    query = parts[0].strip()
    category = parts[1].strip()
    price = parts[2].strip() if len(parts) > 2 else "0"

    filters = {
        "category": category,
        "stock_quantity": {"$gt": 0}
    }
    if price != "0":
        filters["price"] = {"$lte": int(price)}

    # Your search logic here
    print(f"Query: {query}, Category: {category}, Price: {price}")

    # Example of search logic
    query_vector = model.encode(query).tolist()
    search_results = index.query(
        vector=query_vector,
        filter=filters,
        top_k=5,
        include_values=False,
        include_metadata=True
    )

    # score_threshold = 0.35

    # Access the 'matches' key in the search_results
    product_data = [
        result['metadata']
        for result in search_results['matches']  # Use 'matches' here
    ]

    # Print product details
    for i, product in enumerate(product_data):
        print(i, "-", product['name'])
        print()

    return product_data

search_product_tool = Tool(
    name="fetch_product_data",
    func=fetch_product_data,
    description=(
        "Retrieves product details from a vector database based on the user's query, category, and price. "
        "Ensure your inputs are relevant to improve search accuracy. The tool performs a similarity search using "
        "vector embeddings and applies filters to find the most relevant products."
    ),
    input_type="dict",
    output_type="list",
    parameters={
        "inputs": {
            "type": "dict",
            "description": (
                "A dictionary containing 'query', 'category', and 'price' for the search. "
                "'query' is the search query, 'category' is the product category, and 'price' is the maximum price."
            ),
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query or product-related question."
                },
                "category": {
                    "type": "string",
                    "description": "The category to filter products by. Strictly one of these [device, custom, accessories]" 
                },
                "price": {
                    "type": "string",
                    "description": "The maximum price to filter products by."
                }
            },
            "required": ["query"]
        }
    },
    examples=[
        {
            "input": {
                "query": "laptop",
                "category": "device",
                "price": "50000"
            },
            "output": [
                {
                    "name": "Lenovo Legion 5 Pro AMD Ryzen 7 5800H QHD Gaming Laptop",
                    "permalink": "https://egeniestores.com/product/lenovo-legion-5-pro/",
                    "description": "Lenovo Legion 5 Pro - Pro-level performance.",
                    "price": "₹86,700"
                }
            ]
        }
    ]
)


def initialize_langchain_agent(llm1):

    tools = [search_product_tool, company_info_policies_tool]

    memory = ConversationBufferWindowMemory(
        memory_key="chat_history",
        k=10,
        return_messages=True
    )

    conversational_agent = initialize_agent(
        agent = 'chat-conversational-react-description',
        tools = tools,
        llm = llm1,
        verbose = True,
        max_iterations = 3,
        handle_parsing_errors=True,
        early_stopping_method = 'generate',
        memory = memory
    )

    system = """
        # Context:
    - Your name is Lisa, an AI-powered marketing and customer support agent for Egenie4u, a premier e-commerce platform based in India.
    - Only answer questions related to Egenie4u's products and services; ignore all other questions.
    - Your primary mission is to engage, inform, and convert customers aged 18-40.
    - Egenie4u specializes in a wide range of products, particularly in electronics, custom-built PCs, and computer peripherals.

    # Objective:
    Provide a friendly and human-like customer interaction experience, ensuring relevant product recommendations that match the user's query and preferences, while effectively convincing the user to make a purchase.

    # System:
    - Use the search_product_tool for product queries. This tool handles both category and price filters.
    - Only use the following categories for filtering: device, custom, and accessories. Any query outside these categories will not return results.
    - Always provide the search query in the format: "query, category, price" (e.g., "Laptops with good battery life, device, 50000"). **If no price filter is provided by the user, use "0"**.
    - Category Details:
       - device: Includes products like mobiles, laptops, PlayStation, and similar electronic devices.
       - accessories: Includes items like mice, keyboards, cooling pads, and other computer peripherals.
       - custom: Includes only custom-built products crafted by Egenie4u.
    - If the query involves exclusion (e.g., "Not gaming laptop"), reframe the query to effectively filter out these excluded terms while still applying the category filter.
    - Assess the user's question to determine whether to call the tool with the appropriate query parameters. If the category is not one of device, custom, or accessories, do not fetch results.
    - Use this tool when the user asks for information about Egenie4u's company details, history, unique selling points, or policies.
    
    # Task:
    Engage with the user, gather necessary details about their product preferences, and then utilize the tool to retrieve relevant products from the database. Ensure the recommendations are accurate and appealing to the user.

    # Instructions:
    - When providing product recommendations, **avoid starting responses with phrases like "Based on the information" or similar.** Instead, start with phrases like "We have ..." or "Here's ..." to make the response feel more engaging and customer-focused.
    - Follow the guidelines for customer engagement, product search, presenting products, and convincing the user as outlined above.
    - Format the response in a clear and readable manner. Ensure that each product is separated by a line space for better readability.
    - **ALWAYS include the price, product name and a link** to the product page when displaying product recommendations.
    """


    conversational_agent.agent.llm_chain.prompt.messages[0].prompt.template = system
    return conversational_agent

index = initialize_pinecone()

def get_agent():
    llm1 = initialize_first_llm()
    return initialize_langchain_agent(llm1)

agent = get_agent()

while True:

    user_input = input("User: ")
    tic = time.perf_counter()
    response = agent(user_input)
    toc = time.perf_counter()

    print(response['output'])
    print(f"time_taken {toc - tic:0.4f} seconds")

