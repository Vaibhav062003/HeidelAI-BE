from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from agents.egenie.customer_agent.prompts import SYSTEM_MESSAGE
from config.settings import ORG_ID
from database import get_mongo_db
from core.services import services
from services.ai_chatbot import load_agents, clear_agents, load_prompts_into_cache, get_prompt

db = get_mongo_db()

if __name__ == "__main__":
    conversation_history = []
    load_prompts_into_cache()

    load_agents()

    CHAT_AGENT = services.agents[ORG_ID]

    prompt = await get_prompt(ORG_ID)

    system_message = SystemMessage(
        content=SYSTEM_MESSAGE
    )

    conversation_history.append(system_message)

    while True:
        msg = input("User: ")

        if (msg.lower() == "exit" or
            msg.lower() == "quit" or
            msg.lower() == "q"
        ):
            print("Exiting the agent.")
            clear_agents()
            break

        conversation_history.append(HumanMessage(content=msg))

        inputs = {"messages": conversation_history}
        result = CHAT_AGENT.invoke(inputs)
        
        if result.get("final_response"):
            print("Final Response:", result["final_response"])
            conversation_history.append(AIMessage(content=result["final_response"].summary))
        else:
            print("Agent Response:", result["messages"][-1].content)
            conversation_history.append(result["messages"][-1])

