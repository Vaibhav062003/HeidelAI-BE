import random, string, secrets, re, json, os, httpx, jwt, time
from fastapi import FastAPI, HTTPException, Depends, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine
from langchain_community.utilities import SQLDatabase
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.conversation.memory import ConversationBufferWindowMemory
from langchain.agents import Tool, initialize_agent

os.environ["GOOGLE_API_KEY"] = "AIzaSyAUfhIfOVjkaUBIUam4DiX8PhD3617xIT4"
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_API_KEY"] = "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"
os.environ["LANGCHAIN_PROJECT"] = "pr-kindly-self-49"

model = SentenceTransformer('sentence-transformers/all-MiniLM-L12-v2')

def initialize_pinecone():
    pc = Pinecone(api_key="84641b3f-7be7-4a15-aa8e-91552da0ca04")
    index_name = "egenie-products"
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



###################################################################################################

def initialize_database():
    # postgresql://egenie:mTt-zNDpB35pjYZ7lEotLw@chatbot-5648.7s5.aws-ap-south-1.cockroachlabs.cloud:26257/defaultdb
    db_uri = "cockroachdb://egenie:mTt-zNDpB35pjYZ7lEotLw@chatbot-5648.7s5.aws-ap-south-1.cockroachlabs.cloud:26257/defaultdb"

    # Create the SQLAlchemy engine
    engine = create_engine(db_uri)
    
    db = SQLDatabase(engine)
    
    # Create queries table if it doesn't exist
    create_table_query = """
    CREATE TABLE IF NOT EXISTS chat_history (
        id SERIAL PRIMARY KEY,
        user_id VARCHAR(50),
        query TEXT,
        response TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    alter_table_query = """
    ALTER TABLE chat_history CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
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
    db.run(create_table_query)
    db.run(alter_table_query)
    db.run(create_table_users)
    return db

db = initialize_database()
index = initialize_pinecone()
llm1 = initialize_first_llm()
agent = initialize_langchain_agent()

def generate_user_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=10))

           
"""Code modified from below"""
gkey = "86c9abe0-f0a8-4230-9fce-ba54a70c538f"
BEARER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZGVudGlmaWVyIjoyNjk1fQ.kSE16TbPskzEm1mX_RrIlXqWkF97pI9JzkOjVMsXl-Y"

def sanitize_input(value):
    # Sanitize for SQL injection
    sanitized_value = re.sub(r"(['\"\\])", r"\\\1", value)
    # Remove non-UTF-8 characters
    return sanitized_value.encode('utf-8', 'ignore').decode('utf-8')

def store_query_in_db(user_id, query, response):
    sanitized_user_id = sanitize_input(user_id)
    sanitized_query = sanitize_input(query)
    sanitized_response = sanitize_input(response)
        
    insert_query = f"""
    INSERT INTO chat_history (user_id, query, response)
    VALUES ('{sanitized_user_id}','{sanitized_query}', '{sanitized_response}')
    """
    db.run(insert_query)


app = FastAPI()

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "https://egeniestores.com"],
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

def store_user_in_db(db, userName, userEmail):
    insert_query = f"""
    INSERT INTO users (userName, userEmail)
    VALUES ('{userName}', '{userEmail}')
    """
    db.run(insert_query)

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

    store_user_in_db(db,userName, userEmail)

    url = 'https://api.fazpass.com/v1/otp/verify'
    payload = {'otp_id': otp_input.otp_id, 'otp' : otp_input.otp}
    headers = {
        'Authorization': f'Bearer {BEARER_TOKEN}'
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            # Create a JWT token with the payload and secret key
            token_payload = {'otp_id': otp_input.otp_id, 'otp': otp_input.otp}
            token = jwt.encode(token_payload, SECRET_KEY, algorithm="HS256")

            # Get the response data from the OTP verification API
            response_data = response.json()

            # Include the JWT token in the response
            response_data['token'] = token

            return response_data
        else:
            raise HTTPException(status_code=response.status_code, detail=response.text)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    user_id = generate_user_id()
    try:
        while True:
            data = await websocket.receive_text()
            print("Input: ",data)
            tic = time.perf_counter()
            output = agent(data)
            response = output['output']
            print("Response: ",response)
            toc = time.perf_counter()
            print(f"Response took {toc - tic:0.4f} seconds")

            # Store query and response in the database
            store_query_in_db(user_id, data, response)
            
            await websocket.send_text(response)
    except WebSocketDisconnect:
        pass

@app.get("/")
async def get():
    return JSONResponse(content={"message": "WebSocket endpoint is ready. Connect to /ws for WebSocket communication."})