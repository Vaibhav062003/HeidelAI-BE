from datetime import datetime, timedelta
import uuid
from fastapi import HTTPException
import requests

# internal modules
from database import get_mongo_db
from core.logger import get_logger
from config.settings import CASHFREE_URL, CASHFREE_APP_ID, CASHFREE_SECRET_KEY
from typing import Literal

logger = get_logger(__name__)
db = get_mongo_db()


async def create_subscription(
    plan_id: Literal["growth_monthly", "growth_yearly"],
    customer_full_name: str,
    customer_email: str,
    customer_phone: str,
):
    
    # 1. Generate unique IDs and timestamps
    subscription_id = f"sub_{uuid.uuid4()}"
    
    # Set subscription expiry to 10 years 
    expiry_date = (datetime.now() + timedelta(days=365*10)).strftime("%Y-%m-%dT%H:%M:%S+05:30")

    # Subscription session ID expiry
    session_expiry = (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S+05:30")

    payload = {
        "subscription_id": subscription_id,
        "plan_details": {
            "plan_id": f"{plan_id}", 
        },
        "customer_details": {
            "customer_name": customer_full_name,   
            "customer_email": customer_email,
            "customer_phone": customer_phone
        },
        "subscription_expiry_time": expiry_date,
        "subscription_first_charge_time": datetime.utcnow(),
        "subscription_meta": {
            "return_url": f"http://localhost:3000/sign-up?sub_id={subscription_id}&status={{subscriptions_status}}",
            "notification_channel": ["SMS", "EMAIL"],
            "session_id_expiry": session_expiry
        },
        "subscription_tags" : {
            "psp_note": "Monthly Subscription for Growth Plan"
        }
    }

    # 3. Prepare Headers
    headers = {
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
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
        return api_data
        # return {
        #     "payment_session_id": api_data.get("subscription_session_id"),
        #     "subscription_id": api_data.get("subscription_id"),
        #     "cf_subscription_id": api_data.get("cf_subscription_id")
        # }

    except requests.exceptions.RequestException as e:
        # Log the actual error response from Cashfree for debugging
        error_msg = e.response.text if e.response else str(e)
        logger.error(f"Cashfree Error: {error_msg}")
        raise HTTPException(status_code=400, detail=f"Failed to create subscription: {error_msg}")