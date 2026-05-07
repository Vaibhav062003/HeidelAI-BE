from database import get_mongo_db
import time
import requests
from datetime import datetime
from loguru import logger # Using loguru for better logging

# === API Configuration (v20.0 as requested) ===
# Credentials from broadcast_template (1).py
ACCESS_TOKEN = "EAAHPjYclZCTEBQySZCZBJbWo8fbaAxLwutwibVeOGrS5dk1HIAYl6MTDYVAyXW5LbISBtPhuq2gtHovWdYyUd2bZC5Ppd2zsZCvqBtJHBQ8ZAiLtanOqW4IQN9kyKadyAjMRM8vZCO2YkSZC0Ch5zNQrKTzsiAnULbIYvqaJ7a1gSVY5q7EuMqj2Awdv972QXNQfkdWjmGFoJaVTcInqjPMSaLIum3gF0prYHjdXo8XvVb24oTacjH6vG0cO4MVd9BkK6q5DHffpI35tSi5HQ7BQtrK64EHeXsZCLWdciT9YjUzlZAxOoboQZDZD"
PHONE_ID = "882666554940853"
API_VERSION = "v20.0"

# === Template Payload Options ===

# 1. Image Header Template
THANKYOU_REPLY_PAYLOAD = {
    "name": "thankyou_reply",
    "language": {"code": "en"},
    "components": [
        {
            "type": "HEADER",
            "parameters": [
                {
                    "type": "IMAGE",
                    "image": {
                        "link": "https://scontent.whatsapp.net/v/t61.29466-34/639841005_1598700581281688_8141986341136125935_n.png?ccb=1-7&_nc_sid=8b1bef&_nc_ohc=HhP3qVTpGpsQ7kNvwEHVM2W&_nc_oc=AdmQXjeGx9Q0zIWwgJ2NCfHV6b9A4o8YFSiiqh7MS_SF2UGsS0fzyQEMKtxre43YYr_BPwIx1CJbBh1mKkU5Bm-4&_nc_zt=3&_nc_ht=scontent.whatsapp.net&edm=AH51TzQEAAAA&_nc_gid=9qHInY5x-CIiEmpZyVkHRw&_nc_tpa=Q5bMBQE9sEy5lGNoeK3myzvg3I7rJVoZ655tZD0hUkL0NsNW3C-RhofzHTfssydD-cq2xH-7A9oy43aK&oh=01_Q5Aa4AFFyyXdQtrBCSxt8Ts4V7HTVY_xGrrZDDVgnNhqiXHkoA&oe=69DA218F"
                    }
                }
            ]
        }
    ]
}

# 2. Button Template (Summer Camp / Social Links)
SOCIAL_MEDIA_LINKS_PAYLOAD = {
    "name": "summer_camp", # Currently matches the button structure requested
    "language": {"code": "en"}
}

# 3. Another Image Header Template
FOLLOW_UP_VMSC_PAYLOAD = {
    "name": "followup_vmsc",
    "language": {"code": "en"},
    "components": [
        {
            "type": "HEADER",
            "parameters": [
                {
                    "type": "IMAGE",
                    "image": {
                        "link": "https://scontent.whatsapp.net/v/t61.29466-34/639841005_1598700581281688_8141986341136125935_n.png?ccb=1-7&_nc_sid=8b1bef&_nc_ohc=HhP3qVTpGpsQ7kNvwEHVM2W&_nc_oc=AdmQXjeGx9Q0zIWwgJ2NCfHV6b9A4o8YFSiiqh7MS_SF2UGsS0fzyQEMKtxre43YYr_BPwIx1CJbBh1mKkU5Bm-4&_nc_zt=3&_nc_ht=scontent.whatsapp.net&edm=AH51TzQEAAAA&_nc_gid=9qHInY5x-CIiEmpZyVkHRw&_nc_tpa=Q5bMBQE9sEy5lGNoeK3myzvg3I7rJVoZ655tZD0hUkL0NsNW3C-RhofzHTfssydD-cq2xH-7A9oy43aK&oh=01_Q5Aa4AFFyyXdQtrBCSxt8Ts4V7HTVY_xGrrZDDVgnNhqiXHkoA&oe=69DA218F"
                    }
                }
            ]
        }
    ]
}

def get_contacts_from_db():
    db = get_mongo_db()
    conversations_collection = db["conversations_org_2zV2OOhodLocEnTybzR8t9MIYCE"]
    cursor = conversations_collection.find({"platform": "whatsapp"}, {"customer_id": 1, "_id": 0})
    return [doc["customer_id"] for doc in cursor]

def send_template_message(phone_number, template_payload, phone_id, access_token):
    url = f"https://graph.facebook.com/{API_VERSION}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "template",
        "template": template_payload
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        res_json = response.json()
        if response.status_code == 200:
            logger.debug(f"{res_json}")
            logger.success(f"Sent to {phone_number} | Msg ID: {res_json.get('messages')[0]['id']}")
            return True
        else:
            logger.error(f"Failed to send to {phone_number}: {res_json}")
            return False
    except Exception as e:
        logger.exception(f"Exception for {phone_number}: {str(e)}")
        return False

def run_broadcast(contact_list, template_payload, phone_id, access_token, delay_seconds=1):
    logger.info(f"Starting broadcast to {len(contact_list)} contacts...")
    
    stats = {"total": len(contact_list), "sent": 0, "failed": 0}

    for index, number in enumerate(contact_list):
        if send_template_message(number, template_payload, phone_id, access_token):
            stats["sent"] += 1
        else:
            stats["failed"] += 1

        if index < len(contact_list) - 1:
            time.sleep(delay_seconds)

    logger.info("--- Broadcast Complete ---")
    logger.info(f"Total Processed: {stats['total']}")
    logger.success(f"Successful:    {stats['sent']}")
    logger.warning(f"Failed:        {stats['failed']}")

if __name__ == "__main__":
    # 1. Choose template
    template_payload = SOCIAL_MEDIA_LINKS_PAYLOAD  # Change here: THANKYOU_REPLY_PAYLOAD, FOLLOW_UP_VMSC_PAYLOAD
    
    # 2. Transcribed contacts (126 numbers from image)
    target_contacts = [
        "9890896604", "9970284985", "9820518347", "9354595875", "7709776510",
        "9403645837", "7218412131", "9146964489", "9370512058", "8087806237",
        "9960351280", "8468879708", "9766625803", "7709746425", "7448176964",
        "9833060581", "7588930560", "8208398146", "9096029320", "9271379345",
        "9833575323", "9405405647", "7091598416", "9579256040", "8888845856",
        "9022548486", "9699238607", "9673888801", "9422866776", "9604099860",
        "7769888224", "9284054084", "9819556071", "8421278939", "9371647174",
        "9011440808", "8055532056", "9960358151", "9769448566", "9404553815",
        "7757004411", "9527741532", "8433875289", "9011642393", "9619544536",
        "9324315331", "9309013728", "8208576028", "7820960322", "9370001122",
        "8055532056", "9527715132", "9967081105", "9960376660", "8956674634",
        "9096291635", "7389840888", "7263036401", "9822757480", "7718914760",
        "9028951552", "9769164411", "9822084439", "7378892739", "9096976463",
        "9920191812", "9820086561", "9833522720", "9773000900", "9022855510",
        "9889929094", "9421857381", "7721841617", "7038228513", "8263802331",
        "9773953875", "9004048214", "9923972145", "8767176888", "9845932261",
        "9892321386", "9819540657", "8779274602", "7276836403", "8149437151",
        "9987053220", "9823755848", "9960422733", "7304340004", "9665411637",
        "9007450079", "7264316448", "9029781392", "9421506404", "9987430475",
        "9730489641", "9029359389", "8369335258", "9011044535", "9921174548",
        "9404606990", "8374462270", "9922011203", "9967072022", "8208377191",
        "8806526126", "8888152565", "9773455188", "9146164965", "7774819169",
        "7558232828", "7972655834", "7038757996", "9766048654", "8624021911",
        "7350014144", "9673269091", "8055305007", "9833810730", "8356837484",
        "9561650962", "7058506830", "9847411988", "8605687892", "9819916857",
        "7387999547"
    ]

    # Clean and deduplicate numbers
    seen = set()
    final_list = []
    for num in target_contacts:
        clean_num = "".join(filter(str.isdigit, num.strip()))
        if clean_num and clean_num not in seen:
            final_list.append(clean_num)
            seen.add(clean_num)
    
    logger.info(f"Loaded {len(final_list)} unique contacts for broadcast.")
    
    # Run broadcast (comment out to dry run or switch template)
    run_broadcast(final_list, template_payload, PHONE_ID, ACCESS_TOKEN)