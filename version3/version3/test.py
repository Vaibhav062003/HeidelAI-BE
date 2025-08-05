from phi.storage.agent.singlestore import S2AgentStorage
from dotenv import load_dotenv
from phi.model.google import Gemini
from phi.agent import Agent, RunResponse
from sqlalchemy import create_engine
import os
load_dotenv()

# SingleStore Configuration
USERNAME = os.getenv("SINGLESTORE_USERNAME")
PASSWORD = os.getenv("SINGLESTORE_PASSWORD")
HOST = os.getenv("SINGLESTORE_HOST")
PORT = os.getenv("SINGLESTORE_PORT")
DATABASE = os.getenv("SINGLESTORE_DATABASE")
SSL_CERT = os.getenv("SINGLESTORE_SSL_CERT", None)

# SingleStore DB URL
db_url = f"mysql+pymysql://{USERNAME}:{PASSWORD}@{HOST}:{PORT}/{DATABASE}?charset=utf8mb4"
if SSL_CERT:
    db_url += f"&ssl_ca={SSL_CERT}&ssl_verify_cert=true"

# Create a database engine
db_engine = create_engine(db_url)

# Create a storage backend using the Singlestore database
storage = S2AgentStorage(
    # store sessions in the ai.sessions table
    table_name="agent_sessions",
    # db_engine: Singlestore database engine
    db_engine=db_engine,
    # schema: Singlestore schema
    schema=DATABASE,
)

# Add storage to the Agent
agent = Agent(
    model=Gemini(id="gemini-1.5-flash"),
    storage=storage,
    # Set add_history_to_messages=true to add the previous chat history to the messages sent to the Model.
    add_history_to_messages=True,
    # Number of historical responses to add to the messages.
    num_history_responses=5,
    # description="You are a shopping assistant for Egenie4u. The company sells electronics and gadgets.",
    # instructions=["Help user find the perfect product for their needs."],
    description="You are a general purpose agent.",
    markdown=True,
    # show_tool_calls=True,
)

agent.print_response("How many people live in Canada?")
agent.print_response("What is their national anthem called?")
agent.print_response("Which country are we speaking about?")