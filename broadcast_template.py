from database import get_mongo_db
import time
import logging
import requests
from datetime import datetime

API_URL = "https://graph.facebook.com/v17.0/904449942754247/marketing_messages"
ACCESS_TOKEN = "EAAHPjYclZCTEBQBol8EOzg6v4NqjuYGPZBDBZC6h7vvNql78ZCR0zc7tR5vzITZAZCO9XFSihOtMeZAhmkFGOtYpqtAY8b5FYLQEtIteTZBu6i8CJvVZC6IWiTbbSxpKejQm3uRNZAZANWHYbsvTzbQ9Q2EOjKN3rO3XaBMP2XQb4W2LBAZCR95ccPH6cz2wtoVUJCo77xVAIM6rMaSTRXrzIr2NHfphBPxRbrqOELqR9lyoN0xbes56ZAwp71Kw8ysMyh2Hs3iJRjCphEnEYSaNVBsfG6xD9PvyuOMkgcLHbdjmGcUs54lhe2AZDZD"
TEMPLATE_NAME = "thankyou_sale"
LANGUAGE_CODE = "EN"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("whatsapp_broadcast.log"),
        logging.StreamHandler()
    ]
)

db = get_mongo_db()

def get_contacts():
    conversations_collection = db["conversations_org_2zV2OOhodLocEnTybzR8t9MIYCE"]

    cursor = conversations_collection.find({"platform" : "whatsapp"}, {"customer_id": 1, "_id":0})
    my_contacts = [doc["customer_id"] for doc in cursor]
    print("Total fetched: ",len(my_contacts))
    print(my_contacts)
    return my_contacts

def send_template_message(phone_number):
    """
    Sends a single WhatsApp template message via the Meta Graph API.
    Returns True if successful, False otherwise.
    """
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {
                "code": LANGUAGE_CODE
            }
        }
    }

    try:
        response = requests.post(API_URL, json=payload, headers=headers)
        response.raise_for_status() # Raises error for 4xx/5xx codes
        logging.info(f"✅ Sent to {phone_number} | Msg ID: {response.json().get('messages')[0]['id']}")
        return True
    except requests.exceptions.RequestException as e:
        error_msg = e.response.json() if e.response else str(e)
        logging.error(f"❌ Failed to send to {phone_number}: {error_msg}")
        return False
    
def run_broadcast(contact_list, delay_seconds=8):
    """
    Iterates through contacts, sends messages with a delay, and logs stats.
    """
    logging.info(f"🚀 Starting broadcast to {len(contact_list)} contacts...")
    
    stats = {
        "total": len(contact_list),
        "sent": 0,
        "failed": 0
    }

    for index, number in enumerate(contact_list):
        # 1. Send the message
        success = send_template_message(number)
        
        # 2. Update stats
        if success:
            stats["sent"] += 1
        else:
            stats["failed"] += 1

        # 3. Handle Delay (Skip delay after the last message)
        if index < len(contact_list) - 1:
            logging.info(f"⏳ Waiting {delay_seconds}s before next message...")
            time.sleep(delay_seconds)

    # --- Final Report ---
    logging.info("--- Broadcast Complete ---")
    logging.info(f"Total Processed: {stats['total']}")
    logging.info(f"✅ Successful:    {stats['sent']}")
    logging.info(f"❌ Failed:        {stats['failed']}")
    
    return stats

# --- Execution Example ---
if __name__ == "__main__":
    # Example list of numbers (ensure they include country code)
    # In a real scenario, you might fetch this from your MongoDB
    my_contacts = get_contacts()
    
    # run_broadcast(my_contacts)