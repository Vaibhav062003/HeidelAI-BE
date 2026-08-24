import unittest
import sys
import types
import base64
import hashlib
import hmac
import os
import uuid
from unittest.mock import patch
from datetime import datetime, timezone

class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail

sys.modules.setdefault("fastapi", types.SimpleNamespace(HTTPException=HTTPException))
sys.modules.setdefault("database", types.SimpleNamespace(get_mongo_client=lambda: None, get_mongo_db=lambda: types.SimpleNamespace()))

from services.entitlement_service import enforcement_enabled, period_window, recent_periods, validate_limits
from services.entitlement_auth import verify_entitlement_request
from services.internal_request import signed_json


class EntitlementContractTest(unittest.TestCase):
    def test_ist_month_rollover_and_limits(self):
        period, reset = period_window(datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(period, "2027-01")
        self.assertEqual(reset.isoformat(), "2027-01-31T18:30:00+00:00")
        self.assertEqual(validate_limits("AI", {"monthly_messages": 100}), {"monthly_messages": 100})
        self.assertEqual(recent_periods(3, datetime(2027, 1, 15, tzinfo=timezone.utc)), ["2027-01", "2026-12", "2026-11"])
        with self.assertRaises(HTTPException):
            validate_limits("BROADCASTING", {"max_batch_recipients": 101, "monthly_recipients": 100})
        self.assertEqual(validate_limits("SEATS", {"seats": 3}), {"seats": 3})
        self.assertEqual(validate_limits("CHANNELS", {"whatsapp_accounts": 1, "instagram_accounts": 2, "email_accounts": 1}), {"whatsapp_accounts": 1, "instagram_accounts": 2, "email_accounts": 1})

    def test_enforcement_is_fail_closed_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(enforcement_enabled())
        with patch.dict(os.environ, {"ENTITLEMENT_ENFORCEMENT_MODE": "observe"}, clear=True):
            self.assertFalse(enforcement_enabled())

    def test_signed_usage_request_authentication(self):
        body = b'{"account_ids":["org_customer"]}'
        timestamp = "1787400000000"
        request_id = "usage-request"
        secret = "shared-secret"
        canonical = f"POST\n/entitlements/usage\n{timestamp}\n{request_id}\n".encode() + body
        signature = base64.b64encode(hmac.new(secret.encode(), canonical, hashlib.sha256).digest()).decode()
        verify_entitlement_request("POST", "/entitlements/usage", timestamp, request_id, signature, body, secret, now_ms=int(timestamp))
        with self.assertRaises(HTTPException):
            verify_entitlement_request("POST", "/entitlements/usage", timestamp, request_id, "wrong", body, secret, now_ms=int(timestamp))

    def test_product_to_math_body_is_exactly_what_is_signed(self):
        secret = "s" * 32
        request_id = "d8c03f2c-eef8-4d8b-8dd1-a9b63ad5890c"
        body, headers = signed_json({"org_id": "org_customer", "user_input": "hello"}, "/api/v2/chat", secret, request_id)
        canonical = f"POST\n/api/v2/chat\n{headers['X-Timestamp']}\n{request_id}\n".encode() + body
        expected = base64.b64encode(hmac.new(secret.encode(), canonical, hashlib.sha256).digest()).decode()
        self.assertEqual(headers["X-Signature"], expected)
        self.assertEqual(body, b'{"org_id":"org_customer","user_input":"hello"}')

        _, provider_headers = signed_json({"org_id": "org_customer"}, "/api/v2/chat", secret, "wamid.provider-message")
        self.assertEqual(provider_headers["X-Request-Id"], str(uuid.uuid5(uuid.NAMESPACE_URL, "heidelai:/api/v2/chat:wamid.provider-message")))


if __name__ == "__main__":
    unittest.main()
