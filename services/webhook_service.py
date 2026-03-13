# Built-in modules
import datetime

# Internal modules
from loguru import logger

async def calculate_whatsapp_reply_window(message_payload):
    """
    Parses the WhatsApp message payload and returns the calculated 
    expiry datetime in UTC.
    """
    # Extract the base timestamp provided by WhatsApp (Unix epoch)
    timestamp_str = message_payload.get("timestamp")
    if timestamp_str is None:
        redacted_payload = {
            "timestamp": None,
            "message_id": message_payload.get("id") or message_payload.get("message_id"),
            "from": message_payload.get("from"),
        }
        redacted_payload = {k: v for k, v in redacted_payload.items() if v is not None}
        logger.error(
            "Missing 'timestamp' in WhatsApp message payload. Context: {}",
            redacted_payload,
        )
        raise ValueError("Missing 'timestamp' in WhatsApp message payload")
    try:
        message_timestamp = int(timestamp_str)
        message_time_utc = datetime.datetime.fromtimestamp(
            message_timestamp,
            datetime.timezone.utc,
        )
    except (TypeError, ValueError, OSError) as exc:
        logger.error(
            "Invalid 'timestamp' value in WhatsApp message payload: {} (error: {})",
            timestamp_str,
            exc,
        )
        raise ValueError(f"Invalid 'timestamp' value in WhatsApp message payload: {timestamp_str}") from exc
    
    # Check if the message originated from a CTWA ad
    referral = message_payload.get("referral", {})
    is_ad_referral = referral.get("source_type") == "ad"
    logger.info(f"Message timestamp (UTC): {message_time_utc}, Is ad referral: {is_ad_referral}")
    
    # Define window length
    window_hours = 72 if is_ad_referral else 24
    logger.info(f"Using a window of {window_hours} hours for expiry calculation.")
    
    # Calculate the exact expiry time
    expiry_time_utc = message_time_utc + datetime.timedelta(hours=window_hours)
    
    return {"expiry_time_utc": expiry_time_utc, "is_ad_referral": is_ad_referral}