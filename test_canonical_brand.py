import sys
import types
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure stub modules exist in sys.modules so patch and dynamic imports work seamlessly
if "services.wa_service" not in sys.modules:
    mock_wa = types.ModuleType("services.wa_service")
    mock_wa.send_whatsapp_message = AsyncMock()
    mock_wa.create_or_update_whatsapp_conversation = AsyncMock()
    mock_wa.resolve_outbound_whatsapp_connection = MagicMock()
    sys.modules["services.wa_service"] = mock_wa

if "services.team_service" not in sys.modules:
    mock_team = types.ModuleType("services.team_service")
    mock_team.create_ai_escalation = AsyncMock()
    sys.modules["services.team_service"] = mock_team

from services.oos_service import canonical_brand, send_oos_to_dealers


# ==========================================
# 1. Canonical Brand Unit Tests
# ==========================================

def test_canonical_brand_normalization():
    """Verify all normalization rules: case folding, hyphen removal, space collapsing, alias mapping."""
    assert canonical_brand("Lian Li") == "lian li"
    assert canonical_brand("LIAN-LI") == "lian li"
    assert canonical_brand("lian-li") == "lian li"
    assert canonical_brand("lianli") == "lian li"
    assert canonical_brand("  Lian   Li  ") == "lian li"
    assert canonical_brand("  Lian-Li  ") == "lian li"
    assert canonical_brand("NZXT") == "nzxt"
    assert canonical_brand("nzxt") == "nzxt"
    assert canonical_brand("ASUS") == "asus"
    assert canonical_brand("msi") == "msi"
    assert canonical_brand("Lenovo") == "lenovo"
    assert canonical_brand("DELL") == "dell"
    assert canonical_brand("HP") == "hp"
    assert canonical_brand(None) == ""
    assert canonical_brand("") == ""
    assert canonical_brand("   ") == ""


def test_dealer_matching_with_alias_variations():
    """Verify Lian Li, LIAN-LI, lianli, and NZXT select the dealer regardless of directory formatting."""
    dealer_entry = {
        "org_id": "org_tenant_1",
        "name": "Lian Li / NZXT Dealer",
        "phone": "910000000001",
        "brands": "Lian Li,NZXT",
        "active": True,
    }

    dealer_brands = [
        canonical_brand(v)
        for v in dealer_entry["brands"].split(",")
        if v.strip()
    ]
    assert dealer_brands == ["lian li", "nzxt"]

    # All variations of Lian Li and NZXT match
    for brand_variation in ["Lian Li", "LIAN-LI", "lian-li", "lianli", "  Lian   Li  ", "NZXT", "nzxt"]:
        requested_canonical = canonical_brand(brand_variation)
        assert requested_canonical in dealer_brands, f"Failed for {brand_variation}"

    # Non-matching brand does not match
    assert canonical_brand("ASUS") not in dealer_brands
    assert canonical_brand("Corsair") not in dealer_brands


def test_dealer_directory_hyphenated_in_db():
    """Verify matching works even if directory record has hyphens e.g. 'Lian-Li, NZXT'."""
    dealer_entry = {
        "org_id": "org_tenant_1",
        "name": "Custom Dealer",
        "phone": "910000000002",
        "brands": "Lian-Li, NZXT",
        "active": True,
    }

    dealer_brands = [
        canonical_brand(v)
        for v in dealer_entry["brands"].split(",")
        if v.strip()
    ]
    assert dealer_brands == ["lian li", "nzxt"]

    assert canonical_brand("lianli") in dealer_brands
    assert canonical_brand("LIAN-LI") in dealer_brands
    assert canonical_brand("Lian Li") in dealer_brands


# ==========================================
# 2. Async End-to-End Routing Tests
# ==========================================

class FakeAsyncCursor:
    def __init__(self, docs):
        self.docs = list(docs)
        self.idx = 0

    def __aiter__(self):
        self.idx = 0
        return self

    async def __anext__(self):
        if self.idx < len(self.docs):
            doc = self.docs[self.idx]
            self.idx += 1
            return doc
        raise StopAsyncIteration


def _build_mock_async_db(dealer_docs):
    async_db = MagicMock()
    dealer_col = MagicMock()
    oos_col = MagicMock()

    def find_dealers(query):
        matched = []
        for d in dealer_docs:
            if query.get("org_id") and d.get("org_id") != query["org_id"]:
                continue
            if "active" in query and d.get("active") != query["active"]:
                continue
            matched.append(d)
        return FakeAsyncCursor(matched)

    dealer_col.find = find_dealers

    update_result = MagicMock()
    update_result.modified_count = 1
    oos_col.update_one = AsyncMock(return_value=update_result)

    async_db.__getitem__.side_effect = lambda name: dealer_col if name == "dealer_directory" else oos_col
    return async_db, oos_col


async def _async_test_send_oos_to_dealers_lian_li_aliases():
    """Verify Lian Li aliases (LIAN-LI, lianli, spaced) route to the correct tenant dealer with 1 WhatsApp send."""
    org_id = "org_test_1"
    dealer_docs = [
        {
            "org_id": org_id,
            "name": "Lian Li / NZXT Dealer",
            "phone": "910000000001",
            "brands": "Lian Li,NZXT",
            "active": True,
        },
        {
            "org_id": org_id,
            "name": "Asus Dealer",
            "phone": "910000000002",
            "brands": "Asus",
            "active": True,
        },
    ]

    for brand_alias in ["LIAN-LI", "lianli", "  Lian   Li  "]:
        async_db, oos_col = _build_mock_async_db(dealer_docs)
        oos_doc = {
            "query_id": "q_123",
            "conversation_id": "conv_123",
            "raw_query_text": "Looking for a case",
            "extracted_attributes": {"brand": brand_alias},
        }

        with patch("services.oos_service.get_async_mongo_db", return_value=async_db), \
             patch("services.oos_service.get_mongo_db") as mock_get_mongo, \
             patch("services.wa_service.resolve_outbound_whatsapp_connection", return_value={"wa_id": "wa_system", "phone_id": "p_1"}), \
             patch("services.wa_service.create_or_update_whatsapp_conversation", new=AsyncMock(return_value="dealer_conv_1")), \
             patch("services.wa_service.send_whatsapp_message", new=AsyncMock(return_value={"message_ids": ["msg_1"]})) as mock_send_wa:

            mock_get_mongo.return_value[f"conversations_{org_id}"].find_one.return_value = {"whatsapp_id": "wa_system"}

            await send_oos_to_dealers(oos_doc, org_id)

            assert mock_send_wa.call_count == 1, f"Expected exactly 1 send for {brand_alias}"
            call_payload = mock_send_wa.call_args[0][0]
            assert call_payload["recipient_override"] == "910000000001"
            assert call_payload["customer_name"] == "Lian Li / NZXT Dealer"


def test_send_oos_to_dealers_lian_li_aliases():
    asyncio.run(_async_test_send_oos_to_dealers_lian_li_aliases())


async def _async_test_send_oos_to_dealers_nzxt():
    """Verify NZXT selects the configured dealer and dispatches 1 WhatsApp message."""
    org_id = "org_test_1"
    dealer_docs = [
        {
            "org_id": org_id,
            "name": "Lian Li / NZXT Dealer",
            "phone": "910000000001",
            "brands": "Lian Li,NZXT",
            "active": True,
        },
    ]

    async_db, oos_col = _build_mock_async_db(dealer_docs)
    oos_doc = {
        "query_id": "q_nzxt",
        "conversation_id": "conv_nzxt",
        "raw_query_text": "Need NZXT cooler",
        "extracted_attributes": {"brand": "NZXT"},
    }

    with patch("services.oos_service.get_async_mongo_db", return_value=async_db), \
         patch("services.oos_service.get_mongo_db") as mock_get_mongo, \
         patch("services.wa_service.resolve_outbound_whatsapp_connection", return_value={"wa_id": "wa_system", "phone_id": "p_1"}), \
         patch("services.wa_service.create_or_update_whatsapp_conversation", new=AsyncMock(return_value="dealer_conv")), \
         patch("services.wa_service.send_whatsapp_message", new=AsyncMock(return_value={"message_ids": ["msg_1"]})) as mock_send_wa:

        mock_get_mongo.return_value[f"conversations_{org_id}"].find_one.return_value = {"whatsapp_id": "wa_system"}

        await send_oos_to_dealers(oos_doc, org_id)

        assert mock_send_wa.call_count == 1
        assert mock_send_wa.call_args[0][0]["recipient_override"] == "910000000001"


def test_send_oos_to_dealers_nzxt():
    asyncio.run(_async_test_send_oos_to_dealers_nzxt())


async def _async_test_send_oos_to_dealers_tenant_and_inactive_isolation():
    """Verify inactive dealers and dealers from other organizations are never contacted."""
    org_target = "org_target"
    dealer_docs = [
        {
            "org_id": org_target,
            "name": "Target Org Inactive Dealer",
            "phone": "910000000003",
            "brands": "Lian Li",
            "active": False,
        },
        {
            "org_id": "org_different",
            "name": "Other Org Active Dealer",
            "phone": "910000000004",
            "brands": "Lian Li",
            "active": True,
        },
    ]

    async_db, oos_col = _build_mock_async_db(dealer_docs)
    oos_doc = {
        "query_id": "q_iso",
        "conversation_id": "conv_iso",
        "raw_query_text": "Need Lian Li case",
        "extracted_attributes": {"brand": "Lian Li"},
    }

    with patch("services.oos_service.get_async_mongo_db", return_value=async_db), \
         patch("services.team_service.create_ai_escalation", new=AsyncMock()) as mock_escalation, \
         patch("services.wa_service.send_whatsapp_message", new=AsyncMock()) as mock_send_wa:

        await send_oos_to_dealers(oos_doc, org_target)

        # No active dealer in org_target -> zero WhatsApp sends
        assert mock_send_wa.call_count == 0
        # Should escalate with no_dealer_found
        assert mock_escalation.call_count == 1
        escalation_payload = mock_escalation.call_args[0][1]
        assert escalation_payload["inferred_intent"] == "no_dealer_found"
        assert escalation_payload["role"] == "oos_no_dealer"


def test_send_oos_to_dealers_tenant_and_inactive_isolation():
    asyncio.run(_async_test_send_oos_to_dealers_tenant_and_inactive_isolation())


async def _async_test_send_oos_to_dealers_raw_query_fallback_with_spaces_and_hyphens():
    """Verify brand is detected and matched from raw_query when extracted_attributes.brand is empty."""
    org_id = "org_test_1"
    dealer_docs = [
        {
            "org_id": org_id,
            "name": "Lian Li / NZXT Dealer",
            "phone": "910000000001",
            "brands": "Lian Li,NZXT",
            "active": True,
        },
    ]

    raw_queries = [
        "Do you have   LIAN - LI   O11 Dynamic in stock?",
        "Check lianli chassis availability",
        "Need a   nzxt   cooler please",
    ]

    for rq in raw_queries:
        async_db, oos_col = _build_mock_async_db(dealer_docs)
        oos_doc = {
            "query_id": "q_raw",
            "conversation_id": "conv_raw",
            "raw_query_text": rq,
            "extracted_attributes": {},  # brand omitted
        }

        with patch("services.oos_service.get_async_mongo_db", return_value=async_db), \
             patch("services.oos_service.get_mongo_db") as mock_get_mongo, \
             patch("services.wa_service.resolve_outbound_whatsapp_connection", return_value={"wa_id": "wa_system", "phone_id": "p_1"}), \
             patch("services.wa_service.create_or_update_whatsapp_conversation", new=AsyncMock(return_value="dealer_conv_raw")), \
             patch("services.wa_service.send_whatsapp_message", new=AsyncMock(return_value={"message_ids": ["msg_raw"]})) as mock_send_wa:

            mock_get_mongo.return_value[f"conversations_{org_id}"].find_one.return_value = {"whatsapp_id": "wa_system"}

            await send_oos_to_dealers(oos_doc, org_id)

            assert mock_send_wa.call_count == 1, f"Failed fallback dispatch for query: '{rq}'"
            assert mock_send_wa.call_args[0][0]["recipient_override"] == "910000000001"


def test_send_oos_to_dealers_raw_query_fallback_with_spaces_and_hyphens():
    asyncio.run(_async_test_send_oos_to_dealers_raw_query_fallback_with_spaces_and_hyphens())


def run_all_tests():
    test_canonical_brand_normalization()
    test_dealer_matching_with_alias_variations()
    test_dealer_directory_hyphenated_in_db()
    test_send_oos_to_dealers_lian_li_aliases()
    test_send_oos_to_dealers_nzxt()
    test_send_oos_to_dealers_tenant_and_inactive_isolation()
    test_send_oos_to_dealers_raw_query_fallback_with_spaces_and_hyphens()
    print("All 7 canonical brand and routing integration tests passed successfully!")


if __name__ == "__main__":
    run_all_tests()
