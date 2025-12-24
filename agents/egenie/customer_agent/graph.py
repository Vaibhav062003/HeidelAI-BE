from .llm import get_llm
from .tools import tools
from .models import ProductResponse
# from .prompts import SYSTEM_MESSAGE
from config.settings import LLM_MODEL
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from typing import TypedDict, Sequence, Annotated, Union
from langchain_core.messages import BaseMessage

from core.logger import get_logger

logger = get_logger(__name__)

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    final_response: Union[ProductResponse, None]

main_llm = get_llm(
    model=LLM_MODEL,
    temperature=0.2
).bind_tools(tools)

product_llm = get_llm(
    model=LLM_MODEL,
    temperature=0.2,
    structured_output_schema=ProductResponse
)

tool_node = ToolNode(tools)

# --- Agent Nodes ---
def call_main_llm(state: AgentState) -> AgentState:
    response = main_llm.invoke(state["messages"])
    return {"messages": [response]}

def call_product_llm(state: AgentState) -> AgentState:
    response = product_llm.invoke(state["messages"])
    return {"final_response": response}

# --- Conditional Edge Logic ---
def should_call_tools(state: AgentState):
    last_message = state["messages"][-1]
    # print("===Last Message Tool Calls===")
    # print(bool(last_message.tool_calls))
    # print("="*8)
    if last_message.tool_calls:
        return "tools"
    return "end"

# --- Graph Definition ---
workflow = StateGraph(AgentState)
workflow.add_node("main_agent", call_main_llm)
workflow.add_node("tools", tool_node)
workflow.add_node("product_agent", call_product_llm)
workflow.set_entry_point("main_agent")

workflow.add_conditional_edges(
    "main_agent",
    should_call_tools,
    {
        "tools": "tools",
        "end": END
    }
)

workflow.add_edge("tools", "product_agent")
workflow.add_edge("product_agent", END)

EGENIE_CHAT_AGENT = workflow.compile()

logger.info(EGENIE_CHAT_AGENT.get_graph().draw_ascii())

if __name__ == "__main__":
    pass

"""
Docs -

LangGraph's ToolNode handles the tool call by:

    Reading the last AI message that has tool_calls

    Executing the tool (search_products) using the provided args

    Generating a new ToolMessage with the tool output

    Adding this ToolMessage to state["messages"]
"""