import httpx
import jwt
from jwt import PyJWK, PyJWTError
from config.settings import JWKS_URL, JWT_TOKEN_ISSUER, JWT_HEADER_ALGORITHM

cached_keys: dict[str, dict] = {}


async def get_key(kid: str) -> dict | None:
    """Fetch the JWKS key matching given kid, with in-memory caching."""
    if kid not in cached_keys:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.get(JWKS_URL)
            res.raise_for_status()
            jwks = res.json()
            cached_keys.update({key["kid"]: key for key in jwks["keys"]})
    return cached_keys.get(kid)


async def decode_jwt(token: str) -> dict:
    """Decode and verify a JWT using JWKS."""
    try:
        header = jwt.get_unverified_header(token)
        key_data = await get_key(header.get("kid"))
        if not key_data:
            raise PyJWTError("Key not found")

        public_key = PyJWK.from_dict(key_data).key
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[JWT_HEADER_ALGORITHM],
            issuer=JWT_TOKEN_ISSUER,
            options={"verify_exp": True},
        )
        return payload
    except PyJWTError as e:
        raise PyJWTError(f"JWT verification failed: {e}")
