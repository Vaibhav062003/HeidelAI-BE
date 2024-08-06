import psycopg2, os, time
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.conversation.memory import ConversationBufferWindowMemory
from langchain.agents import Tool
from langchain.agents import initialize_agent

os.environ["GOOGLE_API_KEY"] = "AIzaSyAUfhIfOVjkaUBIUam4DiX8PhD3617xIT4"
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_API_KEY"] = "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"
os.environ["LANGCHAIN_PROJECT"] = "pr-kindly-self-49"

# Define your connection parameters
conn_params = {
    "dbname": "egenie",
    "user": "postgres",
    "password": "egeniesql",
    "host": "localhost",
    "port": "5432"
}

# Establish the connection
conn = psycopg2.connect(**conn_params)


model = SentenceTransformer('all-MiniLM-L6-v2')


def initialize_pinecone():
    pc = Pinecone(api_key="84641b3f-7be7-4a15-aa8e-91552da0ca04")

    index_name = "egenie-new"
    
    index = pc.Index(index_name)
    return index


def initialize_first_llm():
    llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)

    return llm

def fetch_product_data(query:str) -> list:
    """
    Searches the vector database for products based on the user's query and returns their metadata.

    Args:
        query (str): The user's search query.

    Returns:
        list: A list of metadata of products that match the search query.
    """
    
    print("Query to the db: ",query)
    query_vector = model.encode(query).tolist()
    search_results = index.query(
        vector=query_vector,
        filter={"stock_quantity": {"$gt": 0}},
        top_k=5,
        include_values=False,  # Set to False to avoid fetching actual vectors, just metadata
        include_metadata=True  # Ensure metadata is included
    )

    score_threshold = 0.4

    # Access the 'matches' key in the search_results
    product_data =  [
        result['metadata']
        for result in search_results['matches']  # Use 'matches' here
        if result['score'] >= score_threshold
    ]

    # product_data = [result['metadata'] for result in search_results['matches']]
    count = 1
    for product in product_data:
        print(count, "-",product['name'])
        count += 1
        print()

    return product_data


search_product_tool = Tool(
    name="fetch_product_data",
    func=fetch_product_data,
    description=(
        "Retrieves product details from a vector database based on the user's query. "
        "Ensure your query is relevant and descriptive to improve search accuracy. "
        "The tool performs a similarity search using vector embeddings to find the most "
        "relevant products."
    ),
    input_type="string",
    output_type="list",
    parameters={
        "query": {
            "type": "string",
            "description": (
                "The search query or product-related question from the user. "
                "Keep in mind that the query should be descriptive and relevant to perform "
                "an accurate similarity search in a vector database."
            )
        }
    },
    examples=[
        {
            "input": "Looking for a gaming laptop with high performance",
            "output": [
                {
                    "name": "Lenovo Legion 5 Pro AMD Ryzen 7 5800H QHD Gaming Laptop",
                    "permalink": "https://egeniestores.com/product/lenovo-legion-5-pro/",
                    "description": "Lenovo Legion 5 Pro -Pro-level performance.",
                    "price": "₹86,700"
                },
            ]
        }
    ]
)


def search_by_price(input_str: str) -> list:
    """
    Searches the SQL database for products under a specified price and returns their metadata.

    Args:
        input_str (str): A single string containing the search query and price limit, separated by a comma.

    Returns:
        list: A list of metadata of products that are priced under the specified amount.
    """
    try:
        query, amount = input_str.rsplit(',', 1)
        amount = int(amount.strip())
        query = query.strip()
    except ValueError:
        raise ValueError("Input should be in the format 'query, amount'.")

    with conn.cursor() as cur:
        sql_query = "SELECT product_id FROM mytable WHERE price < %s AND stock_quantity > 0;"
        cur.execute(sql_query, (amount,))
        products = cur.fetchall()

    
    print("Products: ", products)

    query_vector = model.encode(query).tolist()
    
    search_results = index.query(
        vector=query_vector,
        filter={"product_id": {"$in": [product[0] for product in products]}, "stock_quantity": {"$gt": 0}},
        top_k=5,
        include_values=False,
        include_metadata=True
    )

    score_threshold = 0.4

    # Access the 'matches' key in the search_results
    product_data =  [
        result['metadata']
        for result in search_results['matches']  # Use 'matches' here
        if result['score'] >= score_threshold
    ]

    # product_data = [result['metadata'] for result in search_results['matches']]
    
    return product_data


search_product_by_price_tool = Tool(
    name="search_by_price",
    func=search_by_price,
    description="Searches the SQL database and vector index for products under a specified price and returns their metadata."
                "DO NOT use this tool for queries that asks for products OVER a specified amount. Use the other tool instead.",
    input_type="str",
    output_type="list",
    parameters={
        "input_str": {
            "type": "str",
            "description": "A single string containing the search query and price limit, separated by a comma."
        }
    },
    examples=[
        {
            "input": "Laptops with good battery life, 50000",
            "output": [
                {
                    "name": "Dell Inspiron 3520 Laptop (Intel Core i5-1235U, 8GB RAM, 512GB SSD)",
                    "permalink": "https://egeniestores.com/product/dell-inspiron-3520/",
                    "description": "Dell Inspiron 3520 Laptop",
                    "price": "48500"
                }
            ]
        }
    ]
)


def initialize_langchain_agent(llm1):

    tools = [search_product_tool, search_product_by_price_tool]

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
    - Use the 'search_product_by_price_tool' to search products based on the price. 
    - ALWAYS provide your search query and price limit in the format: "query, amount" (e.g., "Laptops with good battery life, 50000").
    - Use the 'search_product_tool' for product queries and provide relevant recommendations based on user preferences.
    - Assess the user's question to determine whether to call a tool for additional information.
    - *ALWAYS include the link and price* to the product in the response.
    - If the query involves negation or exclusion (e.g., "Not gaming laptop"), reframe the query to ensure it effectively filters out the excluded terms. Construct a query that can be accurately used to search within a vector database.
    
    # Task:
    Engage with the user, gather necessary details about their product preferences, and then utilize the tool to retrieve relevant products from the database. Ensure the recommendations are accurate and appealing to the user.

    # Instructions:
    - Before displaying the products, filter out any irrelevant products based on the user's query. DO NOT get confused with laptops and laptop accessories like cooling pads.
    - When providing product recommendations, **avoid starting responses with phrases like "Based on the information" or similar.** Instead, start with phrases like "We have ..." or "Here's ..." to make the response feel more engaging and customer-focused.
    - Follow the guidelines for customer engagement, product search, presenting products, and convincing the user as outlined above.
    - Check the product names and return only those which are asked for in the query.
    - Format the response in a clear and readable manner. Ensure that each product is separated by a line space for better readability.
    - ALWAYS give the product name, description, price, and a link to the product page when displaying product recommendations.
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
