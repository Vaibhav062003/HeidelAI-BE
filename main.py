# === Built-in Modules ===
from config.settings import INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET

# === Third-party Modules ===
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# === Internal Modules ===
from routers import ig_routes, wa_routes, websockets_routes, webhook_routes, ai_routes, common_routes


# INSTAGRAM LOGIN CODE EXCHANGE
if not INSTAGRAM_APP_ID or not INSTAGRAM_APP_SECRET:
    print("Warning: Instagram credentials not properly configured!")
else:
    print(f"Using Instagram App ID: {INSTAGRAM_APP_ID[:5]}... (truncated)")

app = FastAPI()

app.include_router(ig_routes.router)
app.include_router(wa_routes.router)
app.include_router(webhook_routes.router)
app.include_router(websockets_routes.router)
app.include_router(ai_routes.router)
app.include_router(common_routes.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://egeniestores.com", 
        "https://www.heidelai.com", 
        "https://heidelai.com",
        "https://www.deveopment.heidelai.com", 
        "https://development.heidelai.com",
        "http://localhost", 
        "http://localhost:3000", 
        "http://wordpress.local", 
        "http://wordpress.local:10004", 
        "http://localhost:3000",
        # Add www subdomain
        "https://www.heidelai.com"  
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"]  
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)