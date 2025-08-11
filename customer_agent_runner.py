from agents.customer_agent.graph import CHAT_AGENT
from langchain_core.messages import HumanMessage, AIMessage
from config.settings import ORG_ID
from database import get_mongo_db
from core.globals import _prompt_cache

db = get_mongo_db()

def load_prompts_into_cache():
    """
    Load all prompts from DB into local memory.
    Should be called once at startup or when cache needs refreshing.
    """
    global _prompt_cache
    cursor = db.organizations.find({}, {"org_id": 1, "chatbot_prompt": 1})
    _prompt_cache = {
        doc["org_id"]: doc.get("chatbot_prompt", "You are a helpful assistant.")
        for doc in cursor
    }


def get_prompt(org_id: str) -> str:
    """
    Get the prompt for a given org from cache.
    If not in cache, fetch from DB and update cache.
    """
    if org_id in _prompt_cache:
        return _prompt_cache[org_id]

    doc = db.organizations.find_one(
        {"org_id": org_id},
        {"chatbot_prompt": 1}
    )
    prompt = doc["chatbot_prompt"]  
    _prompt_cache[org_id] = prompt
    return prompt

if __name__ == "__main__":
    conversation_history = []
    load_prompts_into_cache()

    while True:
        msg = input("User: ")

        if (msg.lower() == "exit" or
            msg.lower() == "quit" or
            msg.lower() == "q"
        ):
            print("Exiting the agent.")
            break

        conversation_history.append(HumanMessage(content=msg))
        prompt = get_prompt("ORG_ID")

        inputs = {"system_prompt": prompt, "messages": conversation_history}
        result = CHAT_AGENT.invoke(inputs)
        
        if result.get("final_response"):
            print("Final Response:", result["final_response"])
            conversation_history.append(AIMessage(content=result["final_response"].summary))
        else:
            print("Agent Response:", result["messages"][-1].content)
            conversation_history.append(result["messages"][-1])

