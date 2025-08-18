from config.settings import ORG_ID
from core.services import services
from agents.egenie.customer_agent.graph import EGENIE_CHAT_AGENT

from database import get_mongo_db

db = get_mongo_db()

def load_prompts_into_cache():
    """
    Load all prompts from DB into local memory.
    Should be called once at startup or when cache needs refreshing.
    """
    cursor = db.organizations.find({}, {"org_id": 1, "chatbot_prompt": 1})
    services.prompt_cache = {
        doc["org_id"]: doc.get("chatbot_prompt")
        for doc in cursor
    }

def get_prompt(org_id: str) -> str:
    """Get the prompt for a given org from cache.
    If not in cache, fetch from DB and update cache.
    """
    if org_id in services.prompt_cache:
        return services.prompt_cache[org_id]

    doc = db.organizations.find_one(
        {"org_id": org_id},
        {"chatbot_prompt": 1}
    )
    prompt = doc["chatbot_prompt"] if doc else None
    services.prompt_cache[org_id] = prompt
    return prompt

def update_prompt_in_cache(org_id: str, new_prompt: str):
    """Update the prompt for a given org in cache."""
    services.prompt_cache[org_id] = new_prompt

def load_agents():
    """Load all agents into memory."""
    print("Loading agents...")
    services.agents[ORG_ID] = EGENIE_CHAT_AGENT
    print("Agents loaded.")

def clear_agents():
    """Clear all agents from memory."""
    print("Clearing agents...")
    services.agents.clear()
    print("Agents cleared.")