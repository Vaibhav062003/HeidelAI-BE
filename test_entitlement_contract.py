import unittest
import sys
import types
from datetime import datetime, timezone

class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail

sys.modules.setdefault("fastapi", types.SimpleNamespace(HTTPException=HTTPException))
sys.modules.setdefault("database", types.SimpleNamespace(get_mongo_client=lambda: None, get_mongo_db=lambda: types.SimpleNamespace()))

from services.entitlement_service import period_window, validate_limits


class EntitlementContractTest(unittest.TestCase):
    def test_ist_month_rollover_and_limits(self):
        period, reset = period_window(datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(period, "2027-01")
        self.assertEqual(reset.isoformat(), "2027-01-31T18:30:00+00:00")
        self.assertEqual(validate_limits("AI", {"monthly_messages": 100}), {"monthly_messages": 100})
        with self.assertRaises(HTTPException):
            validate_limits("BROADCASTING", {"max_batch_recipients": 101, "monthly_recipients": 100})


if __name__ == "__main__":
    unittest.main()
