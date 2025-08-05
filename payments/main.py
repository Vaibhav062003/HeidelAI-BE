from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import razorpay
import hmac
import hashlib
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Razorpay test keys
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
TEST_PLAN_ID = os.getenv("TEST_PLAN_ID")

client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "key_id": RAZORPAY_KEY_ID, "plan_id": TEST_PLAN_ID})

@app.post("/create_subscription")
async def create_subscription(plan_id: str = Form(...)):
    subscription = client.subscription.create({
        "plan_id": plan_id,
        "total_count": 12,
        "customer_notify": 1,
    })
    return JSONResponse(content=subscription)

@app.post("/verify")
async def verify_signature(data: dict):
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        (data["razorpay_payment_id"] + "|" + data["razorpay_subscription_id"]).encode(),
        hashlib.sha256
    ).hexdigest()

    if hmac.compare_digest(expected_signature, data["razorpay_signature"]):
        return {"status": "success"}
    return {"status": "failure"}, 400

@app.get("/success", response_class=HTMLResponse)
async def success(request: Request):
    return templates.TemplateResponse("success.html", {"request": request})
