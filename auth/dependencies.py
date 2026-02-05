from typing import Annotated
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

from .service import decode_jwt
from .models import CurrentUserModel
from core.logger import get_logger

security = HTTPBearer()
logger = get_logger(__name__)

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentUserModel:
    token = credentials.credentials
    try:
        payload = await decode_jwt(token)
        logger.info(f"Decoded JWT payload: {payload}") 
        return CurrentUserModel(
            user_id=payload["sub"],
            org_id=payload["o"]["id"],
            role=payload["o"]["rol"],
        )
    except (JWTError, KeyError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


CurrentUser = Annotated[CurrentUserModel, Depends(get_current_user)]
