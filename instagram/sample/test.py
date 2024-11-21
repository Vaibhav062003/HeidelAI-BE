from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import uvicorn
from typing import Optional, Dict
import hmac
import hashlib
import json
from pydantic import BaseModel

# Import equivalent services (these would need to be converted from JS to Python)
from services.receive import Receive  # You'll need to convert these service classes
from services.graph_api import GraphApi
from services.user import User
from services.config import Config
from services.profile import Profile

app = FastAPI()
config = Config()  # Configuration class similar to the JS version

# Serve static files
app.mount("/static", StaticFiles(directory="public"), name="static")

# Setup templates
templates = Jinja2Templates(directory="templates")

# Store users in memory (in production, use a proper database)
users: Dict[str, User] = {}

def verify_request_signature(request: Request, raw_body: bytes) -> bool:
    """Verify that the callback came from Facebook"""
    if "x-hub-signature" not in request.headers:
        print("Couldn't find x-hub-signature in headers")
        return False
    
    signature = request.headers["x-hub-signature"]
    elements = signature.split("=")
    signature_hash = elements[1]
    
    expected_hash = hmac.new(
        config.app_secret.encode('utf-8'),
        raw_body,
        hashlib.sha1
    ).hexdigest()
    
    return hmac.compare_digest(signature_hash, expected_hash)

def is_guest_user(webhook_event: dict) -> bool:
    """Check if the user is a guest user"""
    if "postback" in webhook_event:
        if "referral" in webhook_event["postback"]:
            if "is_guest_user" in webhook_event["postback"]["referral"]:
                return True
    return False

def set_default_user(id: str):
    """Set a default user"""
    user = User(id)
    users[id] = user
    # Set locale to en_US (you'll need to implement i18n handling)

async def receive_and_return(user: User, webhook_event: dict, is_user_ref: bool):
    """Handle receiving and returning messages"""
    receive_message = Receive(user, webhook_event, is_user_ref)
    return await receive_message.handle_message()

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the index page"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = None,
    hub_verify_token: str = None,
    hub_challenge: str = None
):
    """Handle webhook verification"""
    if hub_mode and hub_verify_token:
        if hub_mode == "subscribe" and hub_verify_token == config.verify_token:
            print("WEBHOOK_VERIFIED")
            return Response(content=hub_challenge)
        return Response(status_code=403)
    return Response(status_code=404)

@app.post("/webhook")
async def webhook(request: Request):
    """Handle webhook events"""
    body_bytes = await request.body()
    
    # Verify request signature
    if not verify_request_signature(request, body_bytes):
        raise HTTPException(status_code=403, detail="Invalid signature")
    
    body = await request.json()
    print("📪 Received webhook:")
    print(json.dumps(body, indent=2))
    
    if body.get("object") == "page":
        for entry in body.get("entry", []):
            # Handle page changes
            if "changes" in entry:
                receive_message = Receive()
                if entry["changes"][0]["field"] == "feed":
                    change = entry["changes"][0]["value"]
                    if change["item"] == "post":
                        await receive_message.handle_private_reply(
                            "post_id",
                            change["post_id"]
                        )
                    elif change["item"] == "comment":
                        await receive_message.handle_private_reply(
                            "comment_id",
                            change["comment_id"]
                        )
            
            # Handle messaging events
            for webhook_event in entry.get("messaging", []):
                # Skip uninteresting events
                if "read" in webhook_event:
                    print("Got a read event")
                    continue
                if "delivery" in webhook_event:
                    print("Got a delivery event")
                    continue
                if webhook_event.get("message", {}).get("is_echo"):
                    print(f"Got an echo of our send, mid = {webhook_event['message']['mid']}")
                    continue
                
                sender_psid = webhook_event.get("sender", {}).get("id")
                user_ref = webhook_event.get("sender", {}).get("user_ref")
                guest_user = is_guest_user(webhook_event)
                
                if sender_psid:
                    if sender_psid not in users:
                        if not guest_user:
                            user = User(sender_psid)
                            try:
                                user_profile = await GraphApi.get_user_profile(sender_psid)
                                user.set_profile(user_profile)
                            except Exception as error:
                                print("Profile is unavailable:", error)
                            finally:
                                users[sender_psid] = user
                                await receive_and_return(users[sender_psid], webhook_event, False)
                        else:
                            set_default_user(sender_psid)
                            await receive_and_return(users[sender_psid], webhook_event, False)
                    else:
                        await receive_and_return(users[sender_psid], webhook_event, False)
                elif user_ref:
                    set_default_user(user_ref)
                    await receive_and_return(users[user_ref], webhook_event, True)
        
        return Response(content="EVENT_RECEIVED")
    
    raise HTTPException(status_code=404, detail="Not Found")

@app.get("/profile")
async def setup_profile(
    verify_token: str = None,
    mode: str = None
):
    """Set up the Messenger Profile"""
    if not config.webhook_url.startswith("https://"):
        return Response(content="ERROR - Need a proper API_URL in the .env file")
    
    if not (mode and verify_token):
        raise HTTPException(status_code=404)
    
    if verify_token != config.verify_token:
        raise HTTPException(status_code=403)
    
    profile = Profile()
    response_content = []
    
    if mode in ["webhook", "all"]:
        await profile.set_webhook()
        response_content.append(
            f"<p>✅ Set app {config.app_id} call to {config.webhook_url}</p>"
        )
    
    if mode in ["profile", "all"]:
        await profile.set_thread()
        response_content.append(
            f"<p>✅ Set Messenger Profile of Page {config.page_id}</p>"
        )
    
    if mode in ["personas", "all"]:
        await profile.set_personas()
        response_content.append(f"<p>✅ Set Personas for {config.app_id}</p>")
        response_content.extend([
            "<p>Note: To persist the personas, add the following variables to your environment variables:</p>",
            "<ul>",
            f"<li>PERSONA_BILLING = {config.persona_billing.id}</li>",
            f"<li>PERSONA_CARE = {config.persona_care.id}</li>",
            f"<li>PERSONA_ORDER = {config.persona_order.id}</li>",
            f"<li>PERSONA_SALES = {config.persona_sales.id}</li>",
            "</ul>"
        ])
    
    if mode in ["nlp", "all"]:
        await GraphApi.call_nlp_configs_api()
        response_content.append(
            f"<p>✅ Enabled Built-in NLP for Page {config.page_id}</p>"
        )
    
    if mode in ["domains", "all"]:
        await profile.set_whitelisted_domains()
        response_content.append(
            f"<p>✅ Whitelisted domains: {config.whitelisted_domains}</p>"
        )
    
    if mode == "private-reply":
        await profile.set_page_feed_webhook()
        response_content.append(
            "<p>✅ Set Page Feed Webhook for Private Replies.</p>"
        )
    
    return HTMLResponse(content="".join(response_content))

if __name__ == "__main__":
    # Check environment variables
    config.check_env_variables()
    
    # Start the server
    port = config.port
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True
    )
    
    print(f"The app is listening on port {port}")
    
    if (
        len(config.personas) == 0
        and config.app_url
        and config.verify_token
    ):
        print(
            "Is this the first time running?\n"
            "Make sure to set the both the Messenger profile, persona "
            "and webhook by visiting:\n"
            f"{config.app_url}/profile?mode=all&verify_token={config.verify_token}"
        )
    
    if config.page_id:
        print("Test your app by messaging:")
        print(f"https://m.me/{config.page_id}")