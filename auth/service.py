import httpx
from jose import jwt, jwk, JWTError
from config.settings import JWKS_URL, JWT_TOKEN_ISSUER, JWT_HEADER_ALGORITHM

cached_keys: dict[str, dict] = {}


async def get_key(kid: str) -> dict | None:
    """Fetch the JWKS key matching given kid, with in-memory caching."""
    if not cached_keys:
        async with httpx.AsyncClient() as client:
            res = await client.get(JWKS_URL)
            jwks = res.json()
            cached_keys.update({key["kid"]: key for key in jwks["keys"]})
    return cached_keys.get(kid)


async def decode_jwt(token: str) -> dict:
    """Decode and verify a JWT using JWKS."""
    try:
        header = jwt.get_unverified_header(token)
        key_data = await get_key(header.get("kid"))
        if not key_data:
            raise JWTError("Key not found")

        public_key = jwk.construct(key_data)
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[JWT_HEADER_ALGORITHM],
            issuer=JWT_TOKEN_ISSUER,
            options={"verify_exp": True},
        )
        return payload
    except JWTError as e:
        raise JWTError(f"JWT verification failed: {e}")
