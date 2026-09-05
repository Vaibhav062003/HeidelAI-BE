from pathlib import Path


root = Path(__file__).parent
policy = (root / "services/ai_policy_service.py").read_text()
routes = (root / "api/ai_routes.py").read_text()

assert '"quote_followup_prompt_enabled": organization.get("quote_followup_prompt_enabled") is not False' in policy
assert "if quote_followup_prompt_enabled is not None" in policy
assert "payload.quote_followup_prompt_enabled" in routes
assert 'result["prompt_immediately"] = policy["quote_followup_prompt_enabled"]' in routes

print("Quote follow-up prompt contract passed.")
