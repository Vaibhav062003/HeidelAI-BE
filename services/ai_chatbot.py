from core.globals import _prompt_cache

def update_prompt_in_cache(org_id: str, new_prompt: str):
    _prompt_cache[org_id] = new_prompt