import time, os
from langchain.prompts import PromptTemplate
from langchain_core.tools import tool
from dotenv import load_dotenv
from phi.model.google import Gemini
from phi.agent import Agent, RunResponse
from langchain import hub
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain.agents import AgentExecutor, create_structured_chat_agent
from langchain_core.runnables.history import RunnableWithMessageHistory
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")
LANGCHAIN_TRACING_V2 = os.getenv("LANGCHAIN_TRACING_V2")
LANGCHAIN_ENDPOINT = os.getenv("LANGCHAIN_ENDPOINT")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
LANGCHAIN_PROJECT = os.getenv("LANGCHAIN_PROJECT")
websocket_product_data = {}

model = SentenceTransformer('all-MiniLM-L6-v2')

def initialize_pinecone():
    pc = Pinecone(api_key="84641b3f-7be7-4a15-aa8e-91552da0ca04")

    index_name = "products-test"
    
    index = pc.Index(index_name)
    return index

def initialize_llm():
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0)
    return llm

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
def fetch_product_data(client_id: str, query: str, category: str, price: int = 0) -> str:
    """
    Searches the vector database for products based on the user's query, category, and price.
    There are these fixed categories - "device", "custom", "accessories".

    Args:
        query (str): The user's search query.
        category (str): The category of the product.
        price (int): The price of the product.

    Returns:
        str: A confirmation of the products displayed to the user.
    """

    global websocket_product_data 

    filters = {
        "category": category,
        "stock_quantity": {"$gt": 0}
    }
    if price != 0:
        filters["price"] = {"$lte": int(price)}

    print(f"Query: {query}, Category: {category}, Price: {price}")

    query_vector = model.encode(query).tolist()
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

    product_list = []
    product_string = "Here are the products sent to the user:\n"

    for product in product_data:
        product_string += f"Name: {product['name']}, Price: {product['price']}\n"
        product_dict = {
        "name": product['name'],
        "price": product['price'],
        "permalink": product['permalink'],
        "image_link": product['images'],
        "on_sale": product['on_sale'],
        "regular_price": product['regular_price'],
        }
        product_list.append(product_dict)


    websocket_product_data[client_id] = product_list
    print("Lenght: ", len(websocket_product_data[client_id]))

    return product_string

def initialize_langchain_agent(llm: int, session_id: str):    

    memory = ChatMessageHistory(session_id=session_id)

    tools = [fetch_product_data, fetch_company_info]
    prompt = hub.pull("web-customer-structured-chat-agent-prompt")
    prompt_img = hub.pull("web-image-customer-structured-chat-agent-prompt")

    agent = create_structured_chat_agent(llm, tools, prompt)
    agent_image = create_structured_chat_agent(llm, tools, prompt_img)
    agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)
    agent_executor_img = AgentExecutor(agent=agent_image, tools=tools, verbose=True)

    agent_with_chat_history = RunnableWithMessageHistory(
        agent_executor,
        lambda session_id: memory,
        input_messages_key="input",
        history_messages_key="chat_history",
        handle_parsing_errors=True,
    )

    agent_with_chat_history_img = RunnableWithMessageHistory(
        agent_executor_img,
        lambda session_id: memory,
        input_messages_key="input",
        history_messages_key="chat_history",
        handle_parsing_errors=True,
    )

    return agent_with_chat_history, agent_with_chat_history_img

index = initialize_pinecone()

def get_agent(session_id: str):
    llm = initialize_llm()
    return initialize_langchain_agent(llm, session_id)

agent_executor, image_agent_executor = get_agent("test")

flag = False

while True:
    client_id = "12345"
    user_input = input(f"User: ")
    user_input = f"(client_id: {client_id}): " + user_input
    tic = time.perf_counter()
    image_input = input("Image Input? (y/n): ").lower()
    if (image_input == "y"):
        flag = True
    else:
        flag = False

    if flag:
        agent = Agent(
            model=Gemini(id="gemini-1.5-flash"),
            description="You are a image recognition assistant for Egenie4u. The company sells electronics and gadgets.",
            instructions=["For the image inputs, identify the product in the image and give a phrase to search it in the database. Also give a small description of the product in the image."],
            markdown=True,
        )
        
        #For image inputs
        run: RunResponse = agent.run(
            "The user wants the product in the image, identify the it and give a pharse to search it in the database.",
            # images=["https://egeniestores.com/wp-content/uploads/2024/04/xps5-banner-desktop-new-1-1.jpg.pagespeed.ic.RD_KoqHfXI.webp"],
            # images=["https://images.samsung.com/levant/smartphones/galaxy-s24-ultra/images/galaxy-s24-ultra-highlights-color-titanium-yellow-back-mo.jpg?imbypass=true"],
            images=["D:/Photos/Screen shots BU/2018-novitec-torado-lamborghini-huracan-rwd-front-view-i2.jpg"],
        )

        response = image_agent_executor.invoke(
            {"input": user_input,
            "image": run.content},
            config={"configurable": {"session_id": "user"}},
        )

        products_space = websocket_product_data.get(client_id)

        if products_space:
            response_data = {
                "type": "products",
                "message": response['output'],
                "products": products_space
            }
            websocket_product_data[client_id] = []
        else:
            response_data = {
                "type": "message",
                "message": response['output']
            }

        toc = time.perf_counter()
        response_json = response_data
        print(response_json)
        products_space = []
        print(f"time_taken {toc - tic:0.4f} seconds")
    else:
        response = agent_executor.invoke(
            {"input": user_input},
            config={"configurable": {"session_id": "user"}},
        )

        products_space = websocket_product_data.get(client_id)

        if products_space:
            response_data = {
                "type": "products",
                "message": response['output'],
                "products": products_space
            }
            websocket_product_data[client_id] = []
        else:
            response_data = {
                "type": "message",
                "message": response['output']
            }

        toc = time.perf_counter()
        response_json = response_data
        print(response_json)
        products_space = []
        print(f"time_taken {toc - tic:0.4f} seconds")