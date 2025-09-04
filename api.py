from fastapi import FastAPI
from routers import ig_routes, wa_routes, websockets_routes, webhook_routes, ai_routes, common_routes

def register_routes(app: FastAPI):
    app.include_router(ig_routes.router)
    app.include_router(wa_routes.router)
    app.include_router(webhook_routes.router)
    app.include_router(websockets_routes.router)
    app.include_router(ai_routes.router)
    app.include_router(common_routes.router)