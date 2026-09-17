import asyncio
from unittest.mock import patch

from auth import service


class _Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"keys": [{"kid": "rotated-key", "kty": "RSA"}]}


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def get(self, _url):
        return _Response()


async def main() -> None:
    service.cached_keys.clear()
    service.cached_keys["old-key"] = {"kid": "old-key"}
    with patch.object(service.httpx, "AsyncClient", return_value=_Client()):
        assert (await service.get_key("rotated-key"))["kid"] == "rotated-key"


if __name__ == "__main__":
    asyncio.run(main())
    print("JWKS key rotation cache check passed.")
