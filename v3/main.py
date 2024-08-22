import os, time
from datetime import datetime
from dotenv import load_dotenv
from langchain import hub
from langchain_google_genai import ChatGoogleGenerativeAI
import google.generativeai as genai
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain.agents import AgentExecutor, create_structured_chat_agent
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone
from pydantic import BaseModel
from typing import List

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
genai.configure(api_key=GOOGLE_API_KEY)
# PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
# PINECONE_INDEX = os.getenv("PINECONE_INDEX")

# model = SentenceTransformer('all-MiniLM-L6-v2')

# def initialize_pinecone():
#     pc = Pinecone(api_key=PINECONE_API_KEY)

#     index_name = PINECONE_INDEX
    
#     index = pc.Index(index_name)
#     return index

# def initialize_first_llm():
#     llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)

#     return llm

def get_products():
    """
    Used to get products from DB
    """
    return {
        "product_name": "laptop",
        "price": 20000,
        "permalink": "#link",
        "image_link": "#image"
    }

model = genai.GenerativeModel('gemini-1.5-pro-latest',
                              tools=[get_products],
                              generation_config={"response_mime_type": "application/json"})

prompt = """
        You are a helpful e-commerce assistant. You have access to a tool that retrieves products from the database. When responding to user queries, follow these formats:

        1. **Normal Response:**
        Using this JSON schema:
            type: {"type": "message"}
            data: {"data": str}
        Example:
            {"type": "message", "data": "How can I help you?"}

        2. **Product Response:**
        Using this JSON schema:
            type: {"type": "products"}
            data: {"data": list[{"product_name": str, "price": int, "permalink": str, "image_link": str}]}
        Example:
            {"type": "products", "data": [
                {"product_name": "samsung", "price": 20000, "permalink": "#link1", "image_link": "#image1"},
                {"product_name": "iphone", "price": 100000, "permalink": "#link2", "image_link": "#image2"}
            ]}
        """

model.generate_content(prompt)

# Subsequent queries can be handled like this:
while True:
    user_query = input("User: ")
    
    if user_query.lower() in ["exit", "quit"]:
        break
    
    # Generate content based on the user's query
    response = model.generate_content(user_query)
    
    # Print the generated response
    print("Assistant:", response.text)