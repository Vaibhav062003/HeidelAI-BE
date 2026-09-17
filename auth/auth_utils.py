# auth_utils.py
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from .dependencies import CurrentUser
from .models import CurrentUserModel

async def admin_required(user: CurrentUser):
    if user.role not in {"admin", "org:admin"}:
        raise HTTPException(status_code=403, detail="Admins only")
    return user


async def require_auth(_: CurrentUser):
    pass


def reverification_required(
    user: CurrentUserModel,
    *,
    max_age_minutes: int = 10,
) -> JSONResponse | None:
    """Return Clerk's standard step-up hint when the strongest factor is stale."""
    first_factor, second_factor = user.factor_verification_age
    strongest_available = second_factor if second_factor >= 0 else first_factor
    if 0 <= strongest_available <= max_age_minutes:
        return None
    return JSONResponse(
        status_code=403,
        content={
            "clerk_error": {
                "type": "forbidden",
                "reason": "reverification-error",
                "metadata": {"reverification": "strict"},
            }
        },
    )
