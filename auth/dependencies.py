from typing import Annotated
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

from .service import decode_jwt
from .models import CurrentUserModel


security = HTTPBearer()


async def authenticate_credentials(
    credentials: HTTPAuthorizationCredentials,
) -> CurrentUserModel:
    token = credentials.credentials
    try:
        payload = await decode_jwt(token)
        return CurrentUserModel(
            user_id=payload["sub"],
            org_id=payload["o"]["id"],
            role=payload["o"]["rol"],
            full_name=payload.get("full_name")
        )
    except (JWTError, KeyError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentUserModel:
    user = await authenticate_credentials(credentials)
    request.state.rate_limit_identity = f"auth:{user.org_id}:{user.user_id}"
    return user


CurrentUser = Annotated[CurrentUserModel, Depends(get_current_user)]
