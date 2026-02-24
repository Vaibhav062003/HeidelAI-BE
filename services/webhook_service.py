# Third-party imports
import datetime

# Built-in imports
from database import get_mongo_db, get_async_mongo_db
from core.logger import get_logger

logger = get_logger(__name__)
db = get_mongo_db()

async def calculate_whatsapp_repy_window(message_payload):
    """
    Parses the WhatsApp message payload and returns the calculated 
    expiry datetime in UTC.
    """
    # Extract the base timestamp provided by WhatsApp (Unix epoch)
    timestamp_str = message_payload.get("timestamp")
    message_time_utc = datetime.datetime.fromtimestamp(int(timestamp_str), datetime.timezone.utc)
    
    # Check if the message originated from a CTWA ad
    referral = message_payload.get("referral", {})
    is_ad_referral = referral.get("source_type") == "ad"
    logger.info(f"Message timestamp (UTC): {message_time_utc}, Is ad referral: {is_ad_referral}")
    
    # Define window length
    window_hours = 72 if is_ad_referral else 24
    logger.info(f"Using a window of {window_hours} hours for expiry calculation.")
    
    # Calculate the exact expiry time
    expiry_time_utc = message_time_utc + datetime.timedelta(hours=window_hours)
    
    return expiry_time_utc