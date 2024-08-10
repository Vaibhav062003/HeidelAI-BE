import random, string, secrets, os, httpx, jwt, time
from fastapi import FastAPI, HTTPException, Depends, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.conversation.memory import ConversationBufferWindowMemory
from langchain.agents import Tool, initialize_agent

os.environ["GOOGLE_API_KEY"] = "AIzaSyAUfhIfOVjkaUBIUam4DiX8PhD3617xIT4"
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_API_KEY"] = "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"
os.environ["LANGCHAIN_PROJECT"] = "chatbot:v1.1"

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

    score_threshold = 0.35

    # Access the 'matches' key in the search_results
    product_data = [
        result['metadata']
        for result in search_results['matches']  # Use 'matches' here
        if result['score'] >= score_threshold
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


def initialize_database():
    db_uri = "cockroachdb://egenie:mTt-zNDpB35pjYZ7lEotLw@chatbot-5648.7s5.aws-ap-south-1.cockroachlabs.cloud:26257/defaultdb"

    # Create the SQLAlchemy engine
    engine = create_engine(db_uri, pool_pre_ping=True)
    
    # Create queries table if it doesn't exist
    create_table_query = """
    CREATE TABLE IF NOT EXISTS chat_history (
        id SERIAL PRIMARY KEY,
        user_id VARCHAR(50),
        username TEXT,
        userEmail TEXT,
        query TEXT,
        response TEXT,
        time_taken TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """

    # Create users table if it doesn't exist
    create_table_users = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        userName TEXT,
        userEmail TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """

    with engine.connect() as connection:
        connection.execute(text(create_table_query))
        connection.execute(text(create_table_users))
    
    return engine

engine = initialize_database()
index = initialize_pinecone()

def get_agent():
    llm1 = initialize_first_llm()
    return initialize_langchain_agent(llm1)

agent = get_agent()

def generate_user_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=10))


gkey = "86c9abe0-f0a8-4230-9fce-ba54a70c538f"
BEARER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZGVudGlmaWVyIjoyNjk1fQ.kSE16TbPskzEm1mX_RrIlXqWkF97pI9JzkOjVMsXl-Y"

def store_query_in_db(user_id, username, userEmail, query, response, time_taken, max_retries=3, retry_delay=2):
    insert_query = text("""
    INSERT INTO chat_history (user_id, username, userEmail, query, response, time_taken)
    VALUES (:user_id, :username, :userEmail, :query, :response, :time_taken)
    """)
    
    retries = 0
    while retries < max_retries:
        try:
            with engine.connect() as connection:
                connection.execute(insert_query, {
                    'user_id': user_id,
                    'username': username,
                    'userEmail': userEmail,
                    'query': query,
                    'response': response,
                    'time_taken': time_taken
                })
            print("Data inserted successfully.")
            break  # Exit the loop if the query succeeds
        except OperationalError as e:
            if 'SSL SYSCALL error: EOF detected' in str(e):
                retries += 1
                print(f"SSL SYSCALL error detected. Retrying {retries}/{max_retries}...")
                time.sleep(retry_delay)  # Wait before retrying
            else:
                raise e  # Re-raise if it's not the specific error we're handling

    if retries == max_retries:
        print("Failed to insert data after multiple retries.")

app = FastAPI()

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://egeniestores.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class EmailInput(BaseModel):
    email: str

class OtpInput(BaseModel):
    otp_id: str
    otp: str
    name : str
    email: str

SECRET_KEY = secrets.token_hex(32) 

def create_jwt_token(userName, userEmail, otp_id, otp):
    token_payload = {
        'userName': userName,
        'userEmail': userEmail,  # Include the username in the token payload
        'otp_id': otp_id,
        'otp' : otp
    }
    token = jwt.encode(token_payload, SECRET_KEY, algorithm="HS256")
    return token

def verify_jwt_token(authorization: str = Header(...)) -> str:
    try:
        token = authorization.split(" ")[1]  # Get the token from the "Bearer" prefix
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.get("/protected-route")
async def protected_route(token: str = Depends(verify_jwt_token)):
    return {"success": True, "message": f"Access granted for {token}"}

def store_user_in_db(userName, userEmail, max_retries=3, retry_delay=2):
    insert_query = text("""
    INSERT INTO users (userName, userEmail)
    VALUES (:userName, :userEmail)
    """)
    
    retries = 0
    while retries < max_retries:
        try:
            with engine.connect() as connection:
                connection.execute(insert_query, {'userName': userName, 'userEmail': userEmail})
            print("Data inserted successfully.")
            break  # Exit the loop if the query succeeds
        except OperationalError as e:
            if 'SSL SYSCALL error: EOF detected' in str(e):
                retries += 1
                print(f"SSL SYSCALL error detected. Retrying {retries}/{max_retries}...")
                time.sleep(retry_delay)  # Wait before retrying
            else:
                raise e  # Re-raise if it's not the specific error we're handling

    if retries == max_retries:
        print("Failed to insert data after multiple retries.")

@app.post("/send-mail-otp")
async def send_otp(email_input: EmailInput):
    url = 'https://api.fazpass.com/v1/otp/request'
    payload = {'email': email_input.email, 'gateway_key': gkey}
    headers = {
        'Authorization': f'Bearer {BEARER_TOKEN}'
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            raise HTTPException(status_code=response.status_code, detail=response.text)

@app.post("/verify-mail-otp")
async def send_otp(otp_input: OtpInput):
    userName = otp_input.name
    userEmail = otp_input.email

    store_user_in_db(userName, userEmail)

    url = 'https://api.fazpass.com/v1/otp/verify'
    payload = {'otp_id': otp_input.otp_id, 'otp' : otp_input.otp}
    headers = {
        'Authorization': f'Bearer {BEARER_TOKEN}'
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            # Create a JWT token with the payload and secret key
            # token_payload = {'otp_id': otp_input.otp_id, 'otp': otp_input.otp}
            # token = jwt.encode(token_payload, SECRET_KEY, algorithm="HS256")

            token = create_jwt_token(userName, userEmail, otp_input.otp_id, otp_input.otp)

            # Get the response data from the OTP verification API
            response_data = response.json()

            # Include the JWT token in the response
            response_data['token'] = token

            return response_data
        else:
            raise HTTPException(status_code=response.status_code, detail=response.text)

def extract_token_from_headers(headers):
    # Extract the token from the 'Authorization' header
    authorization = headers.get('Authorization', '')
    if authorization.startswith('Bearer '):
        return authorization.split(' ')[1]
    return None

def decode_jwt_token(token):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, agent=Depends(get_agent)):
    await websocket.accept()

    # Extract token from headers
    token = extract_token_from_headers(websocket.headers)
    if token:
        token_data = decode_jwt_token(token)
        if token_data:
            username = token_data.get('userName', 'Unknown')
            userEmail = token_data.get('userEmail', 'Unknown')
        else:
            username = 'Unknown'
            userEmail = 'Unknown'
    else:
        username = 'Unknown'
        userEmail = 'Unknown'

    user_id = generate_user_id()
    try:
        while True:
            data = await websocket.receive_text()
            print("Input: ", data)
            tic = time.perf_counter()
            output = agent(data)
            response = output['output']
            print("Response: ", response)
            toc = time.perf_counter()
            print(f"Response took {toc - tic:0.4f} seconds")
            time_taken = round(toc - tic, 4)

            # Store query and response in the database
            store_query_in_db(user_id, username, userEmail, data, response, time_taken)
            
            await websocket.send_text(response)
    except WebSocketDisconnect:
        pass


@app.get("/")
async def get():
    return JSONResponse(content={"message": "WebSocket endpoint is ready. Connect to /ws for WebSocket communication."})