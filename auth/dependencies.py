from typing import Annotated
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError

from .service import decode_jwt
from .models import CurrentUserModel


security = HTTPBearer()


def _factor_verification_age(payload: dict) -> tuple[int, int]:
    value = payload.get("fva")
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return (-1, -1)
    try:
        return (int(value[0]), int(value[1]))
    except (TypeError, ValueError):
        return (-1, -1)


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
            full_name=payload.get("full_name"),
            factor_verification_age=_factor_verification_age(payload),
        )
    except (PyJWTError, KeyError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentUserModel:
    user = await authenticate_credentials(credentials)
    request.state.rate_limit_identity = f"auth:{user.org_id}:{user.user_id}"
    return user


CurrentUser = Annotated[CurrentUserModel, Depends(get_current_user)]
