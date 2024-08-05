import csv, json, os
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone, ServerlessSpec
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.conversation.memory import ConversationBufferWindowMemory
from langchain.agents import Tool
from langchain.agents import initialize_agent
import time

os.environ["GOOGLE_API_KEY"] = "AIzaSyAUfhIfOVjkaUBIUam4DiX8PhD3617xIT4"
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_API_KEY"] = "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"
os.environ["LANGCHAIN_PROJECT"] = "pr-kindly-self-49"

model = SentenceTransformer('all-MiniLM-L6-v2')


def initialize_pinecone():
    pc = Pinecone(api_key="84641b3f-7be7-4a15-aa8e-91552da0ca04")

    index_name = "egenie-products"

    if index_name not in pc.list_indexes().names():
        pc.create_index(
        name=index_name,
        dimension=384,
        metric="cosine",
        spec=ServerlessSpec(
            cloud='aws',
            region='us-east-1'
        )
    )
    
    index = pc.Index(index_name)
    return index

def format_product_data(product_data: list) -> str:
    """Formats product data into a JSON string."""
    formatted_products = []
    for product in product_data:
        formatted_products.append({
            "name": product.get("name", ""),
            "description": product.get("description", "").replace('\n', ' ').replace('"', '\\"'),
            "price": product.get("price", ""),
            "permalink": product.get("permalink", "")
        })
    return json.dumps(formatted_products, ensure_ascii=False)

def fetch_product_data(query: str) -> str:
        """Searches the database and returns top 5 products matching the query."""

        print("Query to the db: ",query)
        query_vector = model.encode(query).tolist()
        search_results = index.query(
            vector=query_vector,
            top_k=5,
            include_values=False,  # Set to False to avoid fetching actual vectors, just metadata
            include_metadata=True  # Ensure metadata is included
        )

        product_data = [result['metadata'] for result in search_results['matches']]
        count = 1
        for product in product_data:
            print(count, "-",product['name'])
            count += 1

        product_data = format_product_data(product_data)

        return product_data

def fetch_policies(query: str) -> str:
    return """
    Egenie4u Policies:
    - Easy returns and fast shipping.
    - All products come with GST bills and are brand new, sealed.
    - Warranty claims supported through respective companies using Egenie4u's GST bill.
    """

def fetch_company_info(query: str) -> str:
    return """
    About Egenie4u:
    - 22 years of wholesale expertise translated into competitive retail pricing.
    - Wide range of products, including high-end gaming PCs, consoles, and computer peripherals.
    - Regular promotions and discounts on popular items.
    - Commitment to customer satisfaction and after-sales support.
    """


def initialize_first_llm():
    llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)

    return llm


policies_tool = Tool(
    name="fetch_policies",
    func=fetch_policies,
    description="Fetches Egenie4u's return policies and other relevant policies."
)

company_info_tool = Tool(
    name="fetch_company_info",
    func=fetch_company_info,
    description="Fetches information about Egenie4u, its history, and unique selling points."
)

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


def initialize_langchain_agent():

    tools = [search_product_tool, policies_tool, company_info_tool]

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
    - Assess the user's question to determine whether to call a tool for additional information.
    - Use the 'fetch_policies' tool for policy-related questions.
    - Use the 'fetch_company_info' tool for company-related questions.
    - Use the 'search_product_tool' for product queries and provide relevant recommendations based on user preferences.
    - *ALWAYS include the link* to the product in the response.
    - If the query involves negation or exclusion (e.g., "Not gaming laptop"), reframe the query to ensure it effectively filters out the excluded terms. Construct a query that can be accurately used to search within a vector database.

    # Task:
    Engage with the user, gather necessary details about their product preferences, and then utilize the tool to retrieve relevant products from the database. Ensure the recommendations are accurate and appealing to the user.

    # Instructions:
    - When providing product recommendations, **avoid starting responses with phrases like "Based on the information" or similar.** Instead, start with phrases like "We have ..." or "Here's ..." to make the response feel more engaging and customer-focused.
    - Follow the guidelines for customer engagement, product search, presenting products, and convincing the user as outlined above.
    """

    conversational_agent.agent.llm_chain.prompt.messages[0].prompt.template = system
    return conversational_agent

index = initialize_pinecone()
llm1 = initialize_first_llm()
agent = initialize_langchain_agent()

while True:
    user_input = input("You: ")
    tic = time.perf_counter()
    response = agent(user_input)
    print("AI: ", response['output'])
    toc = time.perf_counter()
    print(f"Response took {toc - tic:0.4f} seconds")

    if user_input.lower() == 'exit':
        break
