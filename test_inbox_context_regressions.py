#!/usr/bin/env python3
"""Focused checks for the Inbox AI-analysis contract."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SERVICE_PATH = ROOT / "services/chat_analysis_service.py"
ROUTES_PATH = ROOT / "api/ai_routes.py"
SERVICE = SERVICE_PATH.read_text()
ROUTES = ROUTES_PATH.read_text()


def load_conversation_text():
    tree = ast.parse(SERVICE)
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "conversation_text")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SERVICE_PATH), "exec"), namespace)
    return namespace["conversation_text"]


render = load_conversation_text()
assert render([
    {"role": "agent", "content": "Latest reply"},
    {"role": "customer", "content": {"image": "ignored"}},
    {"role": "customer", "content": "  Need a laptop  "},
]) == "customer: Need a laptop\nagent: Latest reply"
assert "except HTTPException:" in ROUTES
assert 'status_code=503, detail="AI analysis is temporarily unavailable."' in ROUTES
assert "raise\n" in SERVICE
assert "Analysis result:" not in SERVICE
print("INBOX_CONTEXT_REGRESSIONS_OK")
