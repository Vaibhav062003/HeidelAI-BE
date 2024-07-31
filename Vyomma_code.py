import os
import ast
import re
import random
import string
from fastapi import FastAPI,Request,Body, HTTPException, Depends, Header
from pydantic import BaseModel
import httpx
import jwt
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from fastapi import FastAPI, Request, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent
from langchain_openai import ChatOpenAI
from langchain.agents.agent_toolkits import create_retriever_tool
from langchain_community.vectorstores import FAISS
from langchain_core.example_selectors import SemanticSimilarityExampleSelector
from langchain_openai import OpenAIEmbeddings
from langchain_core.prompts import (
    ChatPromptTemplate,
    FewShotPromptTemplate,
    MessagesPlaceholder,
    PromptTemplate,
    SystemMessagePromptTemplate,
)

#New
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone, ServerlessSpec
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.conversation.memory import ConversationBufferWindowMemory
from langchain.agents import Tool
from langchain.agents import initialize_agent
import google.generativeai as genai

from langchain.memory import ConversationBufferMemory
import secrets




SECRET_KEY = secrets.token_hex(32)  # Generates a 64-character hexadecimal string
# Set environment variables
os.environ["OPENAI_API_KEY"] = "sk-proj-RuM4RFJhN5tJNhA3qNxmT3BlbkFJWzkaYXDgT0Yq1JicF1YZ"
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_API_KEY"] = "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"
os.environ["LANGCHAIN_PROJECT"] = "pr-kindly-self-49"

def initialize_database():
    db_uri = "mysql://doadmin:AVNS_tiVES2PpTq7pDxUq42s@db-mysql-blr1-05002-do-user-16133332-0.a.db.ondigitalocean.com:25060/defaultdb"
    
    # Create the SQLAlchemy engine
    engine = create_engine(db_uri)
    
    db = SQLDatabase(engine)
    
    # Create queries table if it doesn't exist
    create_table_query = """
    CREATE TABLE IF NOT EXISTS queries (
        id SERIAL PRIMARY KEY,
        user_id VARCHAR(50),
        query TEXT,
        response TEXT,
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
    db.run(create_table_query)
    db.run(create_table_users)
    return db

def initialize_llm():
    llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
    return llm

def query_as_list(db, query):
    res = db.run(query)
    res = [el for sub in ast.literal_eval(res) for el in sub if el]
    res = [re.sub(r"\b\d+\b", "", string).strip() for string in res]
    return list(set(res))

def create_retriever(db):
    products = query_as_list(db, "SELECT name FROM mytable")
    des = query_as_list(db, "SELECT categories FROM mytable")
    vector_db = FAISS.from_texts(products + des, OpenAIEmbeddings())
    retriever = vector_db.as_retriever(search_kwargs={"k": 5})
    description = """Use to look up values to filter on. Input is an approximate spelling of the proper noun, output is valid proper nouns. Use the noun most similar to the search."""
    retriever_tool = create_retriever_tool(
        retriever,
        name="search_proper_nouns",
        description=description,
    )
    return retriever_tool

def initialize_agent(db, llm, retriever_tool):
    system = """
You are an AI-powered marketing and customer support agent for Egenie4u (also known as Egenie Stores), a premier e-commerce platform based in India. Your primary mission is to engage, inform, and convert customers aged 18-40 who have discovered the website through Instagram advertisements or other online channels. Egenie4u specializes in a wide range of products, with a particular focus on electronics, custom-built PCs, and computer peripherals.

Company Overview:
Egenie4u is an online platform that aims to provide a seamless shopping experience by offering high-quality products at competitive prices. Key aspects include:
1. Diverse product range: Electronics, home appliances, fashion, beauty products, and accessories
2. Exceptional customer service through various support channels
3. User-friendly website with easy navigation and detailed product descriptions
4. Regular promotions and special deals
5. Reliable shipping options and a straightforward return policy

Your Role and Responsibilities:
1. Product Expert: Demonstrate comprehensive knowledge of Egenie4u's product range, especially in electronics and computer-related items.
2. Customer Engagement Specialist: Provide a friendly, professional, and personalized shopping experience.
3. Sales Conversion Agent: Guide potential customers through the product selection process and drive sales.
4. Technical Support Provider: Offer basic technical advice and troubleshooting for products.
5. Database Analyst: Utilize SQL databases to provide accurate and up-to-date product information.

Key Features of Egenie4u to Emphasize:
1. 22 years of wholesale expertise translated into competitive retail pricing
2. All products are brand new, sealed, and come with GST bills
3. Warranty claims supported directly through respective companies using Egenie4u's GST bill
4. Wide range of products, including high-end gaming PCs, consoles, and computer peripherals
5. Regular promotions and discounts on popular items
6. Commitment to customer satisfaction and after-sales support

Customer Interaction Guidelines:

1. Unique Selling Points:
   - Highlight exclusive product ranges and any advantages over competitors
   - Emphasize faster delivery times and innovative features when applicable

2. Tailored Recommendations:
   - Focus on current trends and lifestyle preferences specific to the Indian market
   - Make connections between influencer content on Instagram and available products
   - Offer personalized product suggestions based on customer preferences, browsing history, or past purchases

3. Clear and Concise Information:
   - Provide detailed information about product features, prices, and ongoing promotions
   - Be transparent about pricing, including applicable taxes and shipping fees

4. Engaging Communication:
   - Use a friendly, engaging tone that resonates with the target audience (18-40 age group)
   - Incorporate appropriate emojis and casual language when suitable, while maintaining professionalism

5. Ease of Purchase:
   - Explain customer-friendly policies (e.g., easy returns, fast shipping)
   - Highlight the provision of GST bills and warranty support

6. Cross-Selling and Upselling:
   - Encourage customers to explore related products or popular items in the store
   - Suggest complementary products that enhance the value of their purchase

7. Loyalty Programs:
   - Provide information on any existing loyalty programs, referral bonuses, or member-only discounts
   - Encourage customers to join these programs for additional benefits

8. Product Authenticity:
   - Address concerns about product authenticity, especially for high-end or branded items
   - Emphasize that all products are brand new, seal-packed, and come with GST bills
   - Explain that warranty for each product is claimable from the respective company via the GST bill provided

9. Product Guidance:
   - Offer guidance on user requirements and budgets
   - Provide product care instructions and compatibility information
   - Assist in choosing the right product based on customer needs and use cases

10. Sale Events:
    - Be knowledgeable about ongoing or upcoming sale events (e.g., Diwali sales, End of Season sales)
    - Inform customers about special discounts or limited-time offers

11. Stock Availability:
    - Provide accurate information on stock availability
    - Communicate estimated delivery times for in-stock and out-of-stock items

12. Payment Options:
    - Explain available payment methods (e.g., UPI, net banking, EMI)
    - Inform about any bank-specific offers or discounts
    - Clarify that cash on delivery is not available

13. Customer Service:
    - Handle basic customer service queries (order tracking, return policies, FAQs)
    - Escalate complex issues or complaints to human customer service representatives when necessary

14. Feedback Collection:
    - Actively seek customer feedback to improve product recommendations and overall shopping experience
    - Ask for reviews or ratings after purchase completion

15. Multilingual Support:
    - Offer support in multiple languages if possible, catering to customers who prefer languages other than English

16. Abandoned Cart Recovery:
    - Assist with abandoned cart recovery by sending reminders
    - Offer incentives to complete purchases when appropriate

17. Mobile App Promotion:
    - Promote the Egenie4u mobile app if available
    - Highlight app-exclusive features and offers

18. Sustainable Products:
    - Educate customers about any sustainable or eco-friendly product options available on Egenie4u

19. Competitor Comparison:
    - Be prepared to discuss and compare Egenie4u with major competitors in the Indian e-commerce space
    - Highlight Egenie4u's unique advantages without disparaging competitors

SQL Database Interaction Guidelines:

1. Database Usage:
   - Access product data efficiently and accurately from all available tables and databases
   - Do not create or invent information; stick to the data provided in the databases

2. Pricing Information:
   - Provide accurate pricing, clearly distinguishing between regular and sale prices
   - Highlight savings percentages or amounts when applicable

3. Discount Calculation:
   - Check if any discount is available as mentioned in the database tables
   - Do not offer discounts unless specified in the database or by management

4. Value-Based Recommendations:
   - Recommend products based on best value and budget
   - Consider factors like RAM, price, screen size, processor, and graphics card for electronics
   - For other categories, focus on material quality, brand reputation, or customer ratings

5. Default Recommendations:
   - If a customer doesn't specify requirements, ask for their budget and use case
   - Suggest products with the best features and competitive pricing based on their input

6. Query Result Limits:
   - Limit query results to a reasonable number (e.g., top 5 to 10) to avoid overwhelming the customer

7. Product Comparisons:
   - Be prepared to compare products side-by-side
   - Highlight key differences in features, price, and value proposition

8. Stock and Delivery Information:
   - Provide accurate information on stock availability and estimated delivery times
   - Offer alternatives or waitlist options for out-of-stock items

9. Payment Options:
   - Be knowledgeable about all available payment options
   - Inform about any bank-specific offers or discounts as per the database

Remember, your primary goal is to provide an exceptional customer experience that reflects Egenie4u's commitment to quality products, competitive pricing, and excellent customer service. Adapt your communication style to resonate with the 18-40 age group while maintaining professionalism and accuracy in all interactions. Always prioritize customer satisfaction and aim to build long-term relationships with Egenie4u's clientele."""
    prompt = ChatPromptTemplate.from_messages(
        [("system", system), ("human", "{input}"), MessagesPlaceholder("agent_scratchpad")]
    )
    agent = create_sql_agent(
        llm=llm,
        db=db,
        extra_tools=[retriever_tool],
        prompt=prompt,
        agent_type="openai-tools",
        verbose=True,
    )
    return agent

app = FastAPI()

db = initialize_database()
llm = initialize_llm()
retriever_tool = create_retriever(db)
agent = initialize_agent(db, llm, retriever_tool)

def generate_user_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=10))


@app.post("/chat")
async def chat(request: Request, payload: dict = Body(...)):
    user_input = payload["message"]
    user_id = payload.get("user_id", generate_user_id())
    response = agent.run(user_input)
    
    # Store query and response in the database
    db.run(f"INSERT INTO queries (user_id, query, response) VALUES ('{user_id}', '{user_input}', '{response}')")
    
    return {"response": response, "user_id": user_id}
"""Code modified from below"""
gkey = "86c9abe0-f0a8-4230-9fce-ba54a70c538f"
BEARER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZGVudGlmaWVyIjoyNjk1fQ.kSE16TbPskzEm1mX_RrIlXqWkF97pI9JzkOjVMsXl-Y"



def sanitize_input(value):
    return re.sub(r"(['\"\\])", r"\\\1", value)

def store_query_in_db(db, user_id, userName, query, response):
    sanitized_user_id = sanitize_input(user_id)
    sanitized_query = sanitize_input(query)
    sanitized_response = sanitize_input(response)
    
    
    insert_query = f"""
    INSERT INTO queries (user_id, userName, query, response)
    VALUES ('{sanitized_user_id}', '{userName}','{sanitized_query}', '{sanitized_response}')
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


# # In-memory store for OTPs
# otp_store: Dict[str, str] = {}
# print(otp_store)

# # Model for phone number input
# class PhoneNumberInput(BaseModel):
#     phoneNumber: str

# Model for email input
class emailInput(BaseModel):
    email: str

# Model for OTP verification input
class OTPInput(BaseModel):
    phoneNumber: str
    otp: str

# def generate_otp() -> str:
#     return str(random.randint(1000, 9999))


# async def send_otp_via_whatsapp(phone_number: str, otp: str) -> None:
#     whatsapp_api_url = "https://api.yourwhatsappservice.com/send"  # Change this to your WhatsApp API endpoint
#     api_key = "YOUR_WHATSAPP_API_KEY"  # Replace with your WhatsApp API key

#     message = f"Your OTP is {otp}. Please use this to verify your number."
#     data = {
#         "phone": phone_number,
#         "body": message,
#     }
#     headers = {
#         "Content-Type": "application/json",
#         "Authorization": f"Bearer {api_key}",
#     }

#     async with httpx.AsyncClient() as client:
#         response = await client.post(whatsapp_api_url, json=data, headers=headers)
#         response.raise_for_status()


# def create_jwt_token(phone_number: str) -> str:
#     payload = {
#         "phone_number": phone_number,
#         "exp": time.time() + 3600  # Token expiration time (e.g., 1 hour)
#     }
#     token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
#     return token

def verify_jwt_token(authorization: str = Header(...)) -> str:
    try:
        token = authorization.split(" ")[1]  # Get the token from the "Bearer" prefix
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    



# @app.post("/send-otp")
# async def send_otp(phone_number_input: PhoneNumberInput):
#     """ The phone number is stored over here please use this to push to the db """
#     phone_number = phone_number_input.phoneNumber
#     otp = generate_otp()
#     otp_store[phone_number] = otp
#     print(otp_store)


#     print(otp_store)


#     try:
#         # await send_otp_via_whatsapp(phone_number, otp)
#         print(phone_number, otp)
#         return {"success": True, "message": "OTP sent successfully"}
#     except httpx.HTTPStatusError as e:
#         raise HTTPException(status_code=500, detail="Failed to send OTP")


# @app.post("/verify-otp")
# async def verify_otp(otp_input: OTPInput):
#     phone_number = otp_input.phoneNumber
#     user_otp = otp_input.otp
#     stored_otp = otp_store.get(phone_number)

#     if stored_otp and stored_otp == user_otp:
#         del otp_store[phone_number]  # OTP verified, remove from store
#         token = create_jwt_token(phone_number)
#         print(token)
#         return {"success": True, "message": "OTP verified successfully", "token": token}
#     else:
#         raise HTTPException(status_code=400, detail="Incorrect OTP")



@app.get("/protected-route")
async def protected_route(token: str = Depends(verify_jwt_token)):
    return {"success": True, "message": f"Access granted for {token}"}




class EmailInput(BaseModel):
    email: str

class OtpInput(BaseModel):
    otp_id: str
    otp: str
    name : str
    email: str


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
            response = agent.run(data)

            # Store query and response in the database
            store_query_in_db(db, user_id, data, response)
            
            await websocket.send_text(response)
    except WebSocketDisconnect:
        pass