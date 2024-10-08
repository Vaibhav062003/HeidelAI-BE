import os
from langchain_core.tools import tool
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import AgentExecutor, create_structured_chat_agent
from langchain_core.runnables.history import RunnableWithMessageHistory
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone
from sentence_transformers import SentenceTransformer
from google.oauth2 import service_account
from google.cloud import firestore
from langchain_google_firestore import FirestoreChatMessageHistory
from langchain import hub
# from langchain.prompts import PromptTemplate
# from langchain_core.prompts.chat import ChatPromptTemplate, MessagesPlaceholder

load_dotenv()

SERVICE_ACCOUNT_FILE = 'whatsapp-chatbot-history-cf43052a413d.json'

LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)

client = firestore.Client(credentials=credentials)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")

websocket_product_data = {}
flag = False

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
    Use this tool if the user asks for contact information or company policies.
    Returns:
        str: A string containing company's information.
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

    For more information, contact us at:
    Contact Number: +918408062973
    Address: 9, 257/b, Tiranga Bhavan, Opposite Dagduseth Ganapati Mandir, Pune - 411002
    GST Number: 27BCLPM6314F1ZK
    """

@tool
def fetch_product_data(client_id: str, query: str, category: str, price: int = 0) -> str:
    """
    Searches the vector database for products based on the user's query, category, and price.
    There are these fixed categories - "device", "custom", "accessories".

    Args:
        client_id (str): The client's unique identifier.
        query (str): The user's search query.
        category (str): The category of the product.
        price (int): The price of the product.

    Returns:
        str: A confirmation of the products displayed to the user.
    """

    global websocket_product_data 
    global search_term 

    search_term = query

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
        "id": product['product_id'],
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

def get_session_history(session_id: str) -> FirestoreChatMessageHistory:

    global flag

    chat_history = FirestoreChatMessageHistory(
        session_id=session_id, collection="HistoryMessages", client=client
    )
    
    if len(chat_history.messages) > 20:
        flag = True

    print(chat_history)
    return chat_history

def initialize_langchain_agent(llm: int):    

    tools = [fetch_product_data, fetch_company_info]

    prompt = hub.pull("customer-structured-chat-agent-prompt")

    agent = create_structured_chat_agent(llm, tools, prompt)
    agent_executor = AgentExecutor.from_agent_and_tools(
                                   agent=agent, 
                                   tools=tools, 
                                   verbose=True,
                                   handle_parsing_errors=True,
                                   max_iterations=100,
                                   )

    agent_with_chat_history = RunnableWithMessageHistory(
    agent_executor,
    get_session_history,
    input_messages_key="input",
    history_messages_key="chat_history",
    )

    return agent_with_chat_history

index = initialize_pinecone()

def get_agent():
    llm = initialize_llm()
    return initialize_langchain_agent(llm)

agent_executor = get_agent()

while True:
    client_id = "ron-test"
    user_input = input(f"User: ")
    user_input = f"(client_id: {client_id}): " + user_input

    response = agent_executor.invoke(
        {"input": user_input},
        config={"configurable": {"session_id": client_id}},
    )

    products_space = websocket_product_data.get(client_id)

    if products_space:
        response_data = {
            "type": "products",
            "response": response['output'],
            "products": products_space
        }
        websocket_product_data[client_id] = []
    else:
        response_data = {
            "type": "message",
            "response": response['output']
        }

    if flag:
        flag = False
        doc_ref = client.collection("HistoryMessages").document(client_id)
        doc = doc_ref.get()
        
        if doc.exists:
            messages = doc.to_dict().get('messages', [])
            print(f"Original messages count: {len(messages)}")
            excess_count = len(messages) - 15
            messages = messages[excess_count:] 
            
            doc_ref.update({'messages': messages})
            print("Document updated with trimmed messages.")

    response_json = response_data
    print(response_json)
    products_space = []


# # Step 1: Fetch Chat History from Firestore
# def fetchChatHistory(user_id, session_id):
#     message_history = FirestoreChatMessageHistory(
#         collection_name="HistoryMessages",
#         session_id=session_id,
#         # user_id=user_id,
#         firestore_client=db  # Make sure `client` is your Firestore client
#     )
#     return message_history

# # Step 2: Define the Prompt with Chat History Placeholder
# def setup_agent_with_history(user_id: str, session_id: str, user_input: str):
#     # Fetch chat history from Firestore
#     chat_message_history = fetchChatHistory(user_id, session_id)

#     # Define the system and human message templates
#     system = "You are an AI assistant for e-commerce."
#     human = '''
#     Chat History: {chat_history}

#     Latest Message: {input}

#     (reminder to respond in a JSON blob no matter what)
#     '''

#     # Create the ChatPromptTemplate with placeholders
#     prompt = ChatPromptTemplate.from_messages(
#         [
#             ("system", system),
#             MessagesPlaceholder("chat_history"),  # Placeholder for history
#             ("human", human),
#         ]
#     )

#     # Step 3: Create Tools and the Agent
#     tools = [fetch_product_data, fetch_company_info]
#     agent = create_structured_chat_agent(llm, tools, prompt)
#     agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

#     # Prepare the input for the agent execution
#     messages = chat_message_history.messages  # Chat history

#     # Update chat history with the new user input
#     chat_message_history.add_user_message(user_input)

#     # Step 4: Pass input and chat history to the agent and get response
#     agent_response = agent_executor.invoke({
#         "input": user_input,
#         "chat_history": messages  # Pass the message history to the agent
#     })

#     # Update chat history with the agent's response
#     chat_message_history.add_ai_message(agent_response)

#     # Return the response
#     return agent_response

# # Step 5: Chat with the Agent
# def chatWithAgent(user_id, session_id, message):
#     # Set up the agent with history
#     response = setup_agent_with_history(user_id, session_id, message)
    
#     # Print the response for debugging
#     print(response)
    
#     return response
