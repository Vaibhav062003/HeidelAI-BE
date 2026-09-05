"""Offline checks against the real connection-list handler; no app credentials."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock

from fastapi import HTTPException
from test_inbox_outage import functions
from services.whatsapp_connection_service import (
    public_connections, public_historical_connections, validate_active_connection_slots,
)


class LegacyConnectionDisplayTests(unittest.IsolatedAsyncioTestCase):
    def listing(self, rows, organization):
        original = copy.deepcopy(rows)
        collection = Mock()
        collection.find.return_value.sort.side_effect = [rows, []]
        db = SimpleNamespace(whatsapp_connections=collection, organizations=Mock())
        db.organizations.find_one.return_value = organization
        scope = functions("api/wa_routes.py", ["list_whatsapp_connections"], db=db,
            whatsapp_connection_limit=lambda _: 50, HTTPException=HTTPException,
            public_connections=public_connections, public_historical_connections=public_historical_connections,
            validate_active_connection_slots=validate_active_connection_slots)
        try:
            return scope["list_whatsapp_connections"](None, SimpleNamespace(org_id="org"))
        finally:
            self.assertEqual(rows, original, "display must not mutate stored records")
            self.assertEqual([call[0] for call in collection.method_calls], ["find", "find"])
            self.assertEqual([call[0] for call in db.organizations.method_calls], ["find_one"])
            for call in collection.find.call_args_list:
                self.assertEqual(call.args[0]["org_id"], "org")

    def test_legacy_line_is_read_only_and_keeps_identity(self):
        for aliases in ({"phone_id": "123", "wa_id": "456"}, {"phone_number_id": "123", "waba_id": "456"}):
            row = {"org_id": "org", **aliases, "user_id": "user", "label": "Existing line"}
            result = self.listing([row], {"wa_id": "456"})
            self.assertEqual(result["connections"][0]["phone_id"], "123")
            self.assertEqual(result["connections"][0]["slot"], 1)
            self.assertTrue(result["connections"][0]["is_primary"])
            self.assertFalse(result["schema_ready"])
            self.assertFalse(result["can_add"])

    def test_ambiguous_or_corrupt_data_still_rejected(self):
        legacy = {"org_id": "org", "phone_id": "123", "wa_id": "456"}
        cases = [
            ([legacy], {"wa_id": "789"}),
            ([legacy], {}),
            ([legacy], {"wa_id": "456", "whatsapp_connection_schema_version": 2}),
            ([legacy, {**legacy, "phone_id": "124"}], {"wa_id": "456"}),
            ([{**legacy, "org_id": "other"}], {"wa_id": "456"}),
            ([{**legacy, "phone_id": ""}], {"wa_id": "456"}),
            ([{**legacy, "phone_number_id": "999"}], {"wa_id": "456"}),
            ([{**legacy, "waba_id": "999"}], {"wa_id": "456"}),
            ([{**legacy, "slot": 2}], {"wa_id": "456"}),
            ([{**legacy, "phone_id": "not-a-phone-id"}], {"wa_id": "456"}),
            ([{**legacy, "slot": 1}, {**legacy, "slot": 2}], {"wa_id": "456"}),
        ]
        for rows, organization in cases:
            with self.subTest(rows=rows, organization=organization):
                with self.assertRaises(HTTPException) as caught:
                    self.listing(rows, organization)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertIn("needs repair", caught.exception.detail)

    def test_migrated_and_empty_organizations_unchanged(self):
        result = self.listing([{"org_id": "org", "phone_id": "123", "wa_id": "456", "slot": 1}],
                              {"wa_id": "456", "whatsapp_connection_schema_version": 2})
        self.assertTrue(result["schema_ready"])
        self.assertTrue(result["can_add"])
        self.assertTrue(self.listing([], {})["can_add"])

    async def test_legacy_disconnect_is_fenced_before_side_effects(self):
        service = SimpleNamespace(disconnect=AsyncMock())
        scope = functions("api/wa_routes.py", ["whatsapp_disconnect"],
            admin_required=AsyncMock(), _whatsapp_schema_ready=lambda _: False,
            _find_owned_whatsapp_connection=lambda *a, **kw: {"phone_id": "123"},
            HTTPException=HTTPException, whatsapp_service=service)
        req = SimpleNamespace(connection_id=None, user_id=None, phone_id="123", phone_number_id=None)
        with self.assertRaises(HTTPException) as caught:
            await scope["whatsapp_disconnect"](None, req, Mock(), SimpleNamespace(org_id="org"))
        self.assertEqual(caught.exception.status_code, 503)
        service.disconnect.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
