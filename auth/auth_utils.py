# auth_utils.py
from fastapi import HTTPException
from .dependencies import CurrentUser

async def admin_required(user: CurrentUser):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    return user


async def require_auth(_: CurrentUser):
    pass