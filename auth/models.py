# models.py
from typing import Optional
from pydantic import BaseModel

class CurrentUserModel(BaseModel):
    user_id: str
    org_id: str
    role: str
    full_name: Optional[str] = "You"
    # Clerk's factor verification age claim, in minutes. -1 means unavailable.
    factor_verification_age: tuple[int, int] = (-1, -1)


class Token(BaseModel):
    access_token: str
    token_type: str
