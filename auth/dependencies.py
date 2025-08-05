# dependencies.py
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt, JWTError, jwk
import httpx
from config.settings import JWKS_URL, JWT_TOKEN_ISSUER, JWT_HEADER_ALGORITHM

security = HTTPBearer()
cached_keys = {}

async def get_key(kid: str):
    if not cached_keys:
        async with httpx.AsyncClient() as client:
            res = await client.get(JWKS_URL)
            jwks = res.json()
            cached_keys.update({key["kid"]: key for key in jwks["keys"]})
            # print("Match:", cached_keys.get(kid))
    return cached_keys.get(kid)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        header = jwt.get_unverified_header(token)
        # print("JWT Header:", header)
        key_data = await get_key(header.get("kid"))
        if not key_data:
            print("Key not found for kid:", header.get("kid"))
            raise JWTError("Key not found")
        public_key = jwk.construct(key_data)
        print("Decoding JWT with public key:")
        payload = jwt.decode(token, 
                             public_key, 
                             algorithms=[JWT_HEADER_ALGORITHM], 
                             issuer=JWT_TOKEN_ISSUER,
                             options={"verify_exp": True})
        # print("Decoded payload:", payload)
        return {
                "user_id": payload.get("sub"),
                "org_id": payload.get("o", {}).get("id"),
                "role": payload.get("o", {}).get("rol")
            }
    except JWTError as e:
        print("JWT error:", e)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

