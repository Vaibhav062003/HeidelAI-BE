import asyncio
import base64
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import uuid
from fastapi import HTTPException, Request
import requests
from dateutil import parser
from pymongo import ReturnDocument

# internal modules
from database import get_mongo_db
from core.logger import get_logger
from config.settings import CASHFREE_URL, CASHFREE_APP_ID, CASHFREE_SECRET_KEY, CASHFREE_VERSION
from typing import Literal

from services.clerk_service import create_organization

logger = get_logger(__name__)
db = get_mongo_db()


async def create_subscription(
    plan_id: Literal["growth_monthly", "growth_yearly"],
    customer_full_name: str,
    customer_email: str,
    customer_phone: str,
):
    # 1. Define Plan Configurations (Amount & Interval)
    # You might want to fetch this from a DB or config file
    PLAN_CONFIG = {
        "growth_monthly": {"amount": 2199.00, "days": 30}, 
        "growth_yearly":  {"amount": 23748.00, "days": 365}
    }
    
    if plan_id not in PLAN_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid Plan ID")

    plan_data = PLAN_CONFIG[plan_id]
    plan_amount = plan_data["amount"]
    cycle_days = plan_data["days"]

    # 2. Generate IDs and Dates
    subscription_id = f"sub_{uuid.uuid4()}"
    now = datetime.utcnow()
    
    # Expiry: 10 years from now
    expiry_date = (now + timedelta(days=365*10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    # Session Expiry: 15 mins
    session_expiry = (now + timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ')

    # FIRST CHARGE DATE: 
    # Since the user pays for the 1st month NOW (via Auth Amount),
    # the first *automated* charge happens at the start of the 2nd cycle.
    next_cycle_date = now + timedelta(days=cycle_days)
    subscription_first_charge_time = next_cycle_date.strftime('%Y-%m-%dT%H:%M:%SZ')

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
        # IMMEDIATE PAYMENT CONFIGURATION
        "authorization_details": {
            "authorization_amount": plan_amount, # User pays this IMMEDIATELY
            "authorization_amount_refund": False # Do not refund it
        },
        "subscription_expiry_time": expiry_date,
        "subscription_first_charge_time": subscription_first_charge_time, # Set to next month/year
        "subscription_meta": {
            "return_url": f"https://development.heidelai.com/payment/status?sub_id={subscription_id}",
            "notification_channel": ["SMS", "EMAIL"],
            "session_id_expiry": session_expiry
        },
        "subscription_tags" : {
            "psp_note": f"Subscription for {plan_id}"
        }
    }

    # 3. Headers
    headers = {
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "x-api-version": "2025-01-01", 
        "Content-Type": "application/json",
        "x-request-id": str(uuid.uuid4())
    }

    # 4. API Call
    try:
        response = requests.post(
            f"{CASHFREE_URL}/subscriptions", 
            json=payload, 
            headers=headers
        )

        # Better Error Logging
        if not response.ok:
            logger.error(f"Cashfree Creation Failed: {response.text}")
            
        response.raise_for_status()
        
        return response.json()

    except requests.exceptions.RequestException as e:
        error_msg = e.response.text if e.response else str(e)
        logger.error(f"Cashfree Error: {error_msg}")
        raise HTTPException(status_code=400, detail=f"Failed to create subscription: {error_msg}")

# Webhook Event Processing

async def verify_cashfree_signature(request: Request):
    signature = request.headers.get("x-webhook-signature")
    timestamp = request.headers.get("x-webhook-timestamp")

    if not signature or not timestamp:
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    raw_body: bytes = await request.body()

    # Cashfree signs: timestamp + raw_body
    signed_payload = timestamp.encode() + raw_body

    computed_hmac = hmac.new(
        CASHFREE_SECRET_KEY.encode(),
        signed_payload,
        hashlib.sha256
    ).digest()

    computed_signature = base64.b64encode(computed_hmac).decode()

    if not hmac.compare_digest(computed_signature, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    
async def build_idempotency_key(event_type: str, data: dict) -> str:
    if data.get("cf_payment_id"):
        ref = data["cf_payment_id"]
    elif data.get("authorization_details", {}).get("authorization_reference"):
        ref = data["authorization_details"]["authorization_reference"]
    elif data.get("cf_subscription_id"):
        ref = data["cf_subscription_id"]
    else:
        ref = hashlib.sha256(json.dumps(data).encode()).hexdigest()

    return f"CASHFREE:{event_type}:{ref}"

async def process_cashfree_event(event_type: str, payload: dict):
    data = payload.get("data", {})
    event_time_str = payload.get("event_time")
    event_time = parser.parse(event_time_str) if event_time_str else datetime.utcnow()

    logger.info(f"Processing Cashfree Event: {event_type}")

    match event_type:
        # --- 1. Lifecycle Events ---
        case "SUBSCRIPTION_STATUS_CHANGED":
            await update_subscription_state(data, event_time)
        
        case "SUBSCRIPTION_AUTH_STATUS":
            await mark_mandate_status(data, event_time)
            
        case "SUBSCRIPTION_CARD_EXPIRY_REMINDER":
            await handle_card_expiry(data, event_time)

        # --- 2. Payment Execution Events ---
        case "SUBSCRIPTION_PAYMENT_NOTIFICATION_INITIATED":
            await mark_payment_initiated(data, event_time)

        case "SUBSCRIPTION_PAYMENT_SUCCESS":
            await mark_payment_success(data, event_time)

        case "SUBSCRIPTION_PAYMENT_FAILED":
            await mark_payment_failed(data, event_time)
            
        case "SUBSCRIPTION_PAYMENT_CANCELLED":
            await mark_payment_cancelled(data, event_time)

        # --- 3. Post-Payment Events ---
        case "SUBSCRIPTION_REFUND_STATUS":
            await update_refund_status(data, event_time)

        case _:
            logger.warning(f"Unhandled Cashfree Event Type: {event_type}")

async def update_subscription_state(data: dict, event_time: datetime):
    """
    Handles: SUBSCRIPTION_STATUS_CHANGED
    Updates the main subscription status (ACTIVE, CANCELLED, ON_HOLD, etc.)
    and the next billing cycle dates.
    """
    sub_details = data.get("subscription_details", {})
    subscription_id = sub_details.get("subscription_id")
    cf_status = sub_details.get("subscription_status")

    if not subscription_id:
        return

    update_fields = {
        "status": cf_status,
        "cf_subscription_id": sub_details.get("cf_subscription_id"),
        "updated_at": datetime.utcnow()
    }

    # Update Cycle Dates if provided
    if sub_details.get("next_schedule_date"):
        next_date = parser.parse(sub_details["next_schedule_date"])
        update_fields["next_billing_date"] = next_date
        update_fields["current_period_end"] = next_date
    
    # Update Expiry if provided
    if sub_details.get("subscription_expiry_time"):
        update_fields["expiry_date"] = parser.parse(sub_details["subscription_expiry_time"])

    # Update Payment Method if available in auth details
    auth_details = data.get("authorization_details", {})
    if auth_details.get("payment_group"):
        update_fields["payment_method_type"] = auth_details.get("payment_group")
    if auth_details.get("payment_method"):
        update_fields["payment_method"] = auth_details.get("payment_method")
    if auth_details.get("payment_id"):
        update_fields["payment_id"] = auth_details.get("payment_id")

    db.subscriptions.update_one(
        {"subscription_id": subscription_id},
        {"$set": update_fields}
    )

async def mark_mandate_status(data: dict, event_time: datetime):
    """
    Handles: SUBSCRIPTION_AUTH_STATUS
    Records whether the user successfully set up the autopay mandate.
    """
    subscription_id = data.get("subscription_id")
    auth_details = data.get("authorization_details", {})
    auth_status = auth_details.get("authorization_status") # e.g., ACTIVE, FAILED

    if not subscription_id:
        return

    # If mandate failed, we might want to flag the subscription
    if auth_status == "FAILED":
        db.subscriptions.update_one(
            {"subscription_id": subscription_id},
            {"$set": {"mandate_active": False, "mandate_status": "FAILED", "updated_at": datetime.utcnow()}}
        )
    else:
        db.subscriptions.update_one(
            {"subscription_id": subscription_id},
            {
                "$set": {
                    "mandate_active": True,
                    "mandate_id": auth_details.get("authorization_reference"),
                    "mandate_status": auth_status,
                    "updated_at": datetime.utcnow()
                }
            }
        )

async def mark_payment_initiated(data: dict, event_time: datetime):
    """
    Handles: SUBSCRIPTION_PAYMENT_NOTIFICATION_INITIATED
    Cashfree sends this before attempting a charge (pre-debit notification).
    We create a 'PENDING' transaction record here.
    """
    cf_payment_id = data.get("cf_payment_id")
    subscription_id = data.get("subscription_id")

    if not cf_payment_id:
        return

    db.transactions.update_one(
        {"cf_payment_id": cf_payment_id},
        {
            "$set": {
                "subscription_id": subscription_id,
                "cf_payment_id": cf_payment_id,
                "amount": data.get("payment_amount"),
                "currency": data.get("payment_currency"),
                "status": "NOTIFICATION_INITIATED", # or PENDING
                "type": "RECURRING", 
                "failure_reason": None,
                "transaction_time": event_time
            },
            "$setOnInsert": {"created_at": datetime.utcnow()}
        },
        upsert=True
    )

async def mark_payment_success(data: dict, event_time: datetime):
    """
    Handles: SUBSCRIPTION_PAYMENT_SUCCESS
    Money has been deducted. This is the most critical event.
    """
    subscription_id = data.get("subscription_id")
    cf_payment_id = data.get("cf_payment_id")

    if not subscription_id or not cf_payment_id:
        return

    # 1. Update/Create Transaction Ledger
    db.transactions.update_one(
        {"cf_payment_id": cf_payment_id},
        {
            "$set": {
                "subscription_id": subscription_id,
                "status": "SUCCESS",
                "amount": data.get("payment_amount"),
                "payment_group": data.get("authorization_details", {}).get("payment_group"),
                "transaction_time": event_time,
                "updated_at": datetime.utcnow()
            },
            "$setOnInsert": {
                "created_at": datetime.utcnow(),
                "type": "INITIAL" if data.get("payment_type") == "AUTH" else "RECURRING"
            }
        },
        upsert=True
    )

    # 2. Update Subscription Last Payment State
    subscription = db.subscriptions.find_one_and_update(
        {"subscription_id": subscription_id},
        {
            "$set": {
                "last_payment_date": event_time,
                "last_payment_status": "SUCCESS",
                "last_payment_cf_id": cf_payment_id,
                # Note: We rely on SUBSCRIPTION_STATUS_CHANGED for updating billing dates
            }
        },
        return_document=ReturnDocument.AFTER
    )

    plan_id = subscription.get("plan_id")
    session_id = subscription.get("session_id")
    clerk_user_id = subscription.get("clerk_user_id")
    
    session = db.sessions.find_one_and_update(
        {"session_id": session_id},
        {"$set": 
            {
                "status": "PAYMENT_SUCCESS",
                "next_step" : "FLOW_COMPLETED"
            }
        },
        return_document=ReturnDocument.AFTER
        )
    
    org_name = f"{session.get('first_name')}'s Organization"
    max_allowed_members = session.get("max_allowed_members", 4)
    owner = {
        "clerk_user_id": clerk_user_id,
        "owner_email": session.get("owner_email"),
        "owner_first_name": session.get("owner_first_name"),
        "owner_last_name": session.get("owner_last_name"),
        "owner_phone_number": session.get("owner_phone_number")
    }
    
    # 3. Create Organization for paid plan subscribers
    organization = await create_organization(
        org_name=org_name,
        max_allowed_members=max_allowed_members,
        session_id=session_id,
        owner=owner,
        plan=plan_id
    )    

async def mark_payment_failed(data: dict, event_time: datetime):
    """
    Handles: SUBSCRIPTION_PAYMENT_FAILED
    """
    cf_payment_id = data.get("cf_payment_id")
    subscription_id = data.get("subscription_id")
    failure_reason = data.get("failure_details", {}).get("failure_reason", "Unknown")

    if not cf_payment_id:
        return

    db.transactions.update_one(
        {"cf_payment_id": cf_payment_id},
        {
            "$set": {
                "subscription_id": subscription_id,
                "status": "FAILED",
                "failure_reason": failure_reason,
                "transaction_time": event_time,
                "updated_at": datetime.utcnow()
            },
            "$setOnInsert": {"created_at": datetime.utcnow()}
        },
        upsert=True
    )

async def mark_payment_cancelled(data: dict, event_time: datetime):
    """
    Handles: SUBSCRIPTION_PAYMENT_CANCELLED
    User cancelled the payment process manually on the gateway.
    """
    cf_payment_id = data.get("cf_payment_id")
    subscription_id = data.get("subscription_id")

    if not cf_payment_id:
        return

    db.transactions.update_one(
        {"cf_payment_id": cf_payment_id},
        {
            "$set": {
                "subscription_id": subscription_id,
                "status": "CANCELLED",
                "failure_reason": "User Cancelled",
                "transaction_time": event_time,
                "updated_at": datetime.utcnow()
            },
            "$setOnInsert": {"created_at": datetime.utcnow()}
        },
        upsert=True
    )

async def update_refund_status(data: dict, event_time: datetime):
    """
    Handles: SUBSCRIPTION_REFUND_STATUS
    Updates the original transaction to reflect the refund.
    """
    cf_payment_id = data.get("cf_payment_id")
    refund_status = data.get("refund_status") # SUCCESS, PENDING, CANCELLED
    
    if not cf_payment_id:
        return

    # We update the existing transaction with refund info
    db.transactions.update_one(
        {"cf_payment_id": cf_payment_id},
        {
            "$set": {
                "is_refunded": True if refund_status == "SUCCESS" else False,
                "refund_status": refund_status,
                "refund_amount": data.get("refund_amount"),
                "refund_id": data.get("refund_id"),
                "refund_note": data.get("refund_note"),
                "refund_time": event_time,
                "updated_at": datetime.utcnow()
            }
        }
    )

async def handle_card_expiry(data: dict, event_time: datetime):
    """
    Handles: SUBSCRIPTION_CARD_EXPIRY_REMINDER
    Deeply nested structure! sent 7 days before card expiry.
    Structure: data -> subscription_status_webhook -> subscription_details
    """
    # 1. Unpack the weird nested structure
    webhook_wrapper = data.get("subscription_status_webhook", {})
    sub_details = webhook_wrapper.get("subscription_details", {})
    
    subscription_id = sub_details.get("subscription_id")
    card_expiry_date = data.get("card_expiry_date")

    if not subscription_id:
        return
        
    logger.info(f"Card expiry reminder for sub {subscription_id} expiring on {card_expiry_date}")

    # 2. Flag in DB so UI can show a banner "Your card is expiring soon"
    db.subscriptions.update_one(
        {"subscription_id": subscription_id},
        {
            "$set": {
                "payment_issue_warning": True,
                "payment_issue_message": f"Card expires on {card_expiry_date}",
                "updated_at": datetime.utcnow()
            }
        }
    )



async def subscription_status_generator(sub_id: str):
    """
    Yields the subscription status.
    Input: subscription_id (Your internal ID, e.g., 'sub_123...')
    """
    retries = 0
    max_retries = 30  # 30 attempts * 2 seconds = 60 seconds is usually enough
    
    while retries < max_retries:
        # --- A. FAST PATH: Check Local DB ---
        # We query the 'subscriptions' collection directly.
        sub_record = db.subscriptions.find_one({"subscription_id": sub_id})
        
        if not sub_record:
            # If record doesn't exist, it means 'create_subscription' failed 
            # or data wasn't saved.
            yield f"data: {json.dumps({'status': 'ERROR', 'message': 'Subscription not found'})}\n\n"
            return

        db_status = sub_record.get("status")
        
        # Check for success
        if db_status == "ACTIVE":
            yield f"data: {json.dumps({'status': 'SUCCESS'})}\n\n"
            break
        
        # Check for failure
        elif db_status in ["CANCELLED", "BANK_REJECTED", "EXPIRED", "FAILED"]:
            yield f"data: {json.dumps({'status': 'FAILED'})}\n\n"
            break

        # --- B. SLOW PATH: Direct API Check (Fallback) ---
        # If DB says 'INITIALIZED' or 'PENDING' for > 6 seconds, ask Cashfree.
        # We use the 'cf_subscription_id' stored in our DB record.
        cf_subscription_id = sub_record.get("cf_subscription_id")
        
        if retries > 3 and cf_subscription_id:
            try:
                headers = {
                    "x-client-id": CASHFREE_APP_ID,
                    "x-client-secret": CASHFREE_SECRET_KEY,
                    "x-api-version": CASHFREE_VERSION
                }
                
                # Fetch status from Cashfree
                response = requests.get(
                    f"{CASHFREE_URL}/subscriptions/{cf_subscription_id}",
                    headers=headers,
                    timeout=5
                )
                
                if response.status_code == 200:
                    data = response.json()
                    cf_status = data.get("subscription_status")
                    
                    if cf_status == "ACTIVE":
                        # CRITICAL: If Cashfree says ACTIVE but DB doesn't, 
                        # it means we missed the webhook. Self-heal the DB now.
                        await update_subscription_state(data) # <--- Re-use your webhook logic function
                        
                        yield f"data: {json.dumps({'status': 'SUCCESS'})}\n\n"
                        break
                        
                    elif cf_status in ["CANCELLED", "BANK_REJECTED"]:
                        yield f"data: {json.dumps({'status': 'FAILED'})}\n\n"
                        break
            except Exception as e:
                print(f"Error polling Cashfree: {e}")

        # If we are here, it's still pending
        yield f"data: {json.dumps({'status': 'PENDING'})}\n\n"
        
        await asyncio.sleep(2)
        retries += 1

    if retries >= max_retries:
        yield f"data: {json.dumps({'status': 'TIMEOUT'})}\n\n"