import os, time
from dotenv import load_dotenv
from langchain import hub
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AIMessage, HumanMessage
from typing import Literal
from langchain_core.tools import tool
from langchain.agents import AgentExecutor, create_structured_chat_agent
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,     
    RunReportRequest,
)


load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")
PRODUCT_ID = os.getenv("PRODUCT_ID")

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'credentials.json'

def initialize_llm():
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0)
    return llm

property_id = PRODUCT_ID

client = BetaAnalyticsDataClient()

@tool
def top_n_most_visited_pages(n : int) -> list:
    """
    Fetches the top n most visited pages of the website.

    Args:
        n (int): The number of pages to fetch.
    Returns:
        list: A list of page URLs from most to least visited.
    """

    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name="pagePathPlusQueryString")],
        metrics=[Metric(name="activeUsers")],
        date_ranges=[DateRange(start_date="yesterday", end_date="today")],
    )
    response = client.run_report(request)

    excluded_pages = ['/']
    pages = []
    for i, row in enumerate(response.rows):
        if row.dimension_values[0].value not in excluded_pages:    
            pages.append(row.dimension_values[0].value)
            if i > n - 1:
                break

    return pages

def initialize_langchain_agent(llm):    

    tools = [top_n_most_visited_pages]

    prompt = hub.pull("hwchase17/structured-chat-agent")

    agent = create_structured_chat_agent(llm, tools, prompt)

    agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

    return agent_executor

def get_agent():
    llm = initialize_llm()
    return initialize_langchain_agent(llm)

agent_executor = get_agent()

chat_history = []

def add_to_chat_history(user_message, ai_response):
    # Append the user's message and AI's response to the chat history
    chat_history.append(HumanMessage(content=user_message))
    chat_history.append(AIMessage(content=ai_response))

while True:

    user_input = input("User: ")
    tic = time.perf_counter()
    response = agent_executor.invoke(
        {
            "input": user_input,
            "chat_history": chat_history,
        }
    )
    toc = time.perf_counter()
    print(response['output'])
    add_to_chat_history(user_input, response["output"])
    print(f"time_taken {toc - tic:0.4f} seconds")


