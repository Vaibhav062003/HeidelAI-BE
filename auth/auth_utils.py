# auth_utils.py
from fastapi import Depends, HTTPException
from .dependencies import get_current_user

async def admin_required(user = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    return user


async def require_auth(_: dict = Depends(get_current_user)):
    pass