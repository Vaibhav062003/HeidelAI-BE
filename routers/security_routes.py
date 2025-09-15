import os
from fastapi import APIRouter

from auth.dependencies import CurrentUser
from database import get_mongo_db

db = get_mongo_db()

router = APIRouter(tags=["security"], prefix="/api/security")

@router.get("/encryption-key")
def get_encryption_key(user: CurrentUser):
    """
    Returns a passphrase string for CryptoJS.AES.encrypt(passphrase-mode).
    Stored in Mongo so the same key is reused per user.
    """
    org_id = user.org_id
    doc = db.organizations.find_one({"org_id": org_id})

    if "aes_passphrase" not in doc:
        
        new_passphrase = os.urandom(16).hex()
        db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"aes_passphrase": new_passphrase}}
        )
        passphrase = new_passphrase
    else:
        passphrase = doc["aes_passphrase"]

    return {"key": passphrase}