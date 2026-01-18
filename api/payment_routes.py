from datetime import datetime, timedelta
import uuid
from fastapi import APIRouter, File, Form, HTTPException, Depends, Response, Query, UploadFile
import requests
from pydantic import BaseModel

# internal modules
from database import get_mongo_db
from core.logger import get_logger
from schemas.models import SubscriptionRequest
from config.settings import CASHFREE_URL, CLIENT_ID, CLIENT_SECRET

logger = get_logger(__name__)
db = get_mongo_db()

router = APIRouter(prefix="/api/payments", tags=["Payment"])

@router.post("/create-subscription")
async def create_subscription(data: SubscriptionRequest):
    
    # 1. Generate unique IDs and timestamps
    # Cashfree requires a unique subscription_id for every request
    subscription_id = f"sub_{uuid.uuid4()}"
    
    # Set subscription expiry (Required field). 
    # Here we set it to 10 years from now for a "perpetual" SaaS sub.
    expiry_date = (datetime.now() + timedelta(days=365*10)).strftime("%Y-%m-%dT%H:%M:%S+05:30")

    # 2. Construct the Payload based on the OpenAPI Spec
    # We define the plan details inline so you don't need to pre-create it in the dashboard.
    payload = {
        "subscription_id": subscription_id,
        "plan_details": {
            "plan_id": f"{data.plan_id}_monthly", # Unique ID for this plan configuration
            "plan_name": "Heidel AI Growth Plan",
            "plan_type": "PERIODIC",              # PERIODIC = Auto-debit
            "plan_amount": data.amount,           # The amount to charge (2199)
            "plan_currency": "INR",
            "plan_interval_type": "MONTH",        # Charge every Month
            "plan_intervals": 1,                  # 1 Month interval
            "plan_max_cycles": 120                # Max 120 cycles (10 years)
        },
        "customer_details": {
            "customer_name": data.customer_id,    # Or pass actual name if available
            "customer_email": data.customer_email,
            "customer_phone": data.customer_phone
        },
        "subscription_expiry_time": expiry_date,
        "subscription_meta": {
             # Where to redirect the user after they complete the auth in the modal
             "return_url": f"http://localhost:3000/dashboard?status={{subscriptions_status}}&sub_id={subscription_id}"
        }
    }

    # 3. Prepare Headers
    # Note: x-api-version is crucial. Using the one from your spec: 2025-01-01
    headers = {
        "x-client-id": CLIENT_ID,
        "x-client-secret": CLIENT_SECRET,
        "x-api-version": "2025-01-01", 
        "Content-Type": "application/json",
        "x-request-id": str(uuid.uuid4())
    }

    # 4. Make the API Call to Cashfree
    try:
        response = requests.post(
            f"{CASHFREE_URL}/subscriptions", 
            json=payload, 
            headers=headers
        )

        logger.info(f"Cashfree Response: {response.status_code} - {response.json()}")
        
        # Raise error if status is not 200
        response.raise_for_status()
        
        api_data = response.json()
        
        # 5. Return the session ID to the Frontend
        # IMPORTANT: The frontend SDK expects 'payment_session_id'. 
        # For subscriptions, Cashfree returns 'subscription_session_id', so we map it.
        return {
            "payment_session_id": api_data.get("subscription_session_id"),
            "subscription_id": api_data.get("subscription_id"),
            "cf_subscription_id": api_data.get("cf_subscription_id")
        }

    except requests.exceptions.RequestException as e:
        # Log the actual error response from Cashfree for debugging
        error_msg = e.response.text if e.response else str(e)
        logger.error(f"Cashfree Error: {error_msg}")
        raise HTTPException(status_code=400, detail=f"Failed to create subscription: {error_msg}")