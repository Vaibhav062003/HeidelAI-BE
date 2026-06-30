# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HeidelAI Backend — a FastAPI application serving as the backend for a multi-channel customer communication platform. It handles WhatsApp, Instagram DMs, WebSocket real-time messaging, AI-powered chatbot agents, automation workflows, and Google Sheets integrations.

## Development Commands

```bash
# Install dependencies
poetry install

# Run the dev server
poetry run uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Run a script
poetry run python script.py

# Add a dependency
poetry add <package-name>
```

There are no test suites or linters configured in this project.

## Architecture

### Entry Point & Lifecycle

- `main.py` — FastAPI app setup, CORS, rate limiting, exception handlers
- `core/lifespan.py` — Startup/shutdown: loads AI agents into memory, starts schedulers (automation, Google Sheets), recovers stale bulk jobs
- `core/services.py` — Global singleton `services` holding all in-memory state (WebSocket connections, agent instances, prompt cache, dedup sets)

### Request Flow

Routes in `api/` are thin controllers that delegate to `services/` for business logic. Auth is handled via `auth/dependencies.py` (Clerk-based JWT verification).

### AI Agents (LangGraph)

- `agents/egenie/customer_agent/` — Main customer-facing chatbot built with LangGraph. Uses a StateGraph with tool-calling LLM nodes (`graph.py`), tools (`tools.py`), and structured output models (`models.py`).
- `agents/followup_agent/` — Follow-up scheduling agent
- `agents/support_agent/` — Support agent
- `services/ai_chatbot.py` — Agent loading/clearing, prompt caching. Agents are keyed by org_id in `services.agents`.

### Platform Services

- `services/platforms/base_platform_service.py` — Abstract base for messaging platforms
- `services/platforms/whatsapp_service.py` and `instagram_service.py` — Platform-specific implementations
- `services/wa_service.py`, `services/ig_service.py` — Higher-level WhatsApp/Instagram business logic

### Data Layer

- `database/__init__.py` — Lazy singleton connections for MongoDB (sync via PyMongo, async via Motor) and Pinecone vector DB
- MongoDB is the primary datastore (`MONGODB_URI`, `MONGODB_CLUSTER`)
- Pinecone for vector similarity (product search, RAG)
- Supabase used for some Instagram DM storage

### Configuration

- `config/settings.py` — All env vars loaded from `.env` via `python-dotenv`
- `ENV` variable (`prod`/`dev`) controls CORS origins and docs visibility

### Real-time Communication

- WebSocket management via `services/websocket_service.py` and tracked in `core/services.py`
- SSE routes in `api/sse_routes.py`

### Scheduling

- `core/scheduler.py` — APScheduler instance
- `services/automation_scheduler.py` — Automation job scheduler
- `services/google_sheets_scheduler.py` — Google Sheets polling scheduler

## Deployment

- Pushes to `dev` trigger CI/CD via GitHub Actions → Docker build → deploy to Koyeb (dev environment)
- Pushes to `main` deploy to production Koyeb service
- Docker image uses multi-stage build with Python 3.12-slim
- Production runs: `uvicorn main:app --host 0.0.0.0 --port 8000`

## Key Patterns

- The `services` singleton in `core/services.py` is the shared in-memory state for WebSocket tracking, agent instances, and dedup caches. Access it via `from core.services import services`.
- AI agents are compiled LangGraph `CompiledStateGraph` instances stored per-org in `services.agents[org_id]`.
- Message deduplication uses `OrderedDict` with TTL-based expiry (`processed_message_ids`, `processed_comment_ids`).
- Logging uses `loguru` with IST timezone formatting.
