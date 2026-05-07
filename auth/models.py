# models.py
from pydantic import BaseModel

class CurrentUserModel(BaseModel):
    user_id: str
    org_id: str
    role: str
    full_name: str


class Token(BaseModel):
    access_token: str
    token_type: str