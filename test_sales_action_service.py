import asyncio
import os
import sys
from types import SimpleNamespace
from pymongo.errors import DuplicateKeyError


for key in (
    "LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT", "AZURE_OPENAI_SUBSCRIPTION_KEY",
    "AZURE_OPENAI_ENDPOINT", "AZURE_DEPLOYMENT",
):
    os.environ.setdefault(key, "test")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGODB_CLUSTER", "test")

from services import sales_action_service as actions


class Jobs:
    def __init__(self):
        self.docs = {}

    async def find_one(self, query):
        return self.docs.get(query.get("_id"))

    async def insert_one(self, document):
        if document["_id"] in self.docs:
            raise DuplicateKeyError("duplicate")
        self.docs[document["_id"]] = dict(document)

    async def update_one(self, query, update, upsert=False):
        key = query["_id"]
        document = self.docs.setdefault(key, {"_id": key})
        if len(document) == 1:
            document.update(update.get("$setOnInsert", {}))
        document.update(update.get("$set", {}))
        for field, amount in update.get("$inc", {}).items():
            document[field] = document.get(field, 0) + amount
        for field in update.get("$unset", {}):
            document.pop(field, None)

    async def find_one_and_update(self, query, update, return_document=None):
        document = self.docs.get(query["_id"])
        expected_status = query.get("status")
        if not document:
            return None
        if isinstance(expected_status, str) and document.get("status") != expected_status:
            return None
        if isinstance(expected_status, dict) and document.get("status") == expected_status.get("$ne"):
            return None
        if query.get("claim_id") is not None and document.get("claim_id") != query["claim_id"]:
            return None
        if "$or" in query and document.get("status") not in {"blocked", "failed"}:
            return None
        old = dict(document)
        document.update(update.get("$set", {}))
        for field, amount in update.get("$inc", {}).items():
            document[field] = document.get(field, 0) + amount
        for field in update.get("$unset", {}):
            document.pop(field, None)
        return document if return_document is not None else old


class DB:
    def __init__(self):
        self.jobs = Jobs()

    def __getitem__(self, name):
        return self.jobs


class ProductCollection:
    def __init__(self, document):
        self.documents = document if isinstance(document, list) else [document]

    async def find_one(self, query):
        return self.documents[0] if self.documents else None

    def find(self, query):
        return self

    def limit(self, length):
        self.length = length
        return self

    async def to_list(self, length):
        return self.documents[:length]


class ProductDB:
    def __init__(self):
        self.organizations = ProductCollection({"sales_catalogue_org_id": "catalogue"})
        self.collections = {
            "product_catalogue": ProductCollection({"product_id": "P1", "model_no": "ABC"}),
            "products_catalogue": ProductCollection({"unique_identifier": "ABC", "stock": 4,
                                                       "source": "google_sheet"}),
        }

    def __getitem__(self, name):
        return self.collections[name]


class PricingAuditCollection:
    def __init__(self):
        self.docs = {}

    async def update_one(self, query, update, upsert=False):
        if upsert and query["_id"] not in self.docs:
            self.docs[query["_id"]] = dict(update["$setOnInsert"])


class PricingAuditDB:
    def __init__(self):
        self.audit = PricingAuditCollection()

    def __getitem__(self, _name):
        return self.audit


def test_stock_count_requires_a_real_number():
    assert actions._stock_count({"stock": 4}) == 4
    assert actions._stock_count({"stock": "1,000"}) == 1000
    assert actions._stock_count({"in_stock": True}) is None


def test_v5_selection_resolves_to_the_live_sheet_product(monkeypatch):
    monkeypatch.setattr(actions, "get_async_mongo_db", ProductDB)
    product = asyncio.run(actions._product("catalogue", "P1"))
    assert product["unique_identifier"] == "ABC"
    assert product["source"] == "google_sheet"


def test_ambiguous_live_product_match_is_not_exact(monkeypatch):
    database = ProductDB()
    database.collections["products_catalogue"] = ProductCollection([
        {"unique_identifier": "ABC", "stock": 4},
        {"unique_identifier": "ABC", "stock": 2},
    ])
    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    assert asyncio.run(actions._product("catalogue", "P1")) is None


def test_billing_intent_is_explicit_and_handles_hinglish():
    assert actions.is_invoice_draft_intent("please bill")
    assert actions.is_invoice_draft_intent("bill karo")
    assert actions.is_invoice_draft_intent("bill kara do")
    assert actions.is_invoice_draft_intent("भेजो", has_source=True)
    assert not actions.is_invoice_draft_intent("bhejo")
    assert not actions.is_invoice_draft_intent("bill mat bhejo")
    assert not actions.is_invoice_draft_intent("old bill bhejo")
    assert not actions.is_invoice_draft_intent("bill kitna hoga?")


def test_sales_action_pricing_modes_are_off_shadow_and_review(monkeypatch):
    now = actions.datetime.now(actions.timezone.utc)
    product = {
        "unique_identifier": "SKU-1",
        "approved_floor_price": 70_000,
        "target_selling_price": 75_000,
        "currency": "INR",
        "gst_rate": 18,
        "selling_gst_inclusive": True,
        "stock_available": 5,
        "price_source_sheet_id": "sheet-1",
        "price_verified_at": now,
        "price_version": "v1",
    }
    case = {
        "org_id": "org",
        "case_id": "case-1",
        "conversation_id": "whatsapp_1",
    }
    database = PricingAuditDB()
    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)

    async def settings(mode):
        return {"mode": mode}

    monkeypatch.setattr(actions, "get_pricing_settings", lambda _org_id: settings("off"))
    off_price = asyncio.run(actions._sales_action_quote_price(
        case, product, 2, 70_000, "message-off",
    ))
    assert off_price == 70_000
    assert database.audit.docs == {}

    monkeypatch.setattr(actions, "get_pricing_settings", lambda _org_id: settings("shadow"))
    shadow_price = asyncio.run(actions._sales_action_quote_price(
        case, product, 2, 70_000, "message-shadow",
    ))
    assert shadow_price == 70_000
    shadow_audit = next(iter(database.audit.docs.values()))
    assert shadow_audit["mode"] == "shadow"
    assert shadow_audit["action"] == "review_required"
    assert "quantity_not_single" in shadow_audit["reasons"]

    monkeypatch.setattr(actions, "get_pricing_settings", lambda _org_id: settings("review"))
    try:
        asyncio.run(actions._sales_action_quote_price(
            case, product, 2, 70_000, "message-review-block",
        ))
    except actions.SalesActionBlocked as exc:
        assert "quantity_not_single" in exc.customer_message
    else:
        raise AssertionError("Review mode accepted a multi-quantity automatic quote")

    target_price = asyncio.run(actions._sales_action_quote_price(
        case, product, 1, 70_000, "message-review-allow",
    ))
    assert target_price == 75_000
    allowed = [doc for doc in database.audit.docs.values() if doc["source_message_id"] == "message-review-allow"]
    assert len(allowed) == 1
    assert allowed[0]["action"] == "auto_send"
    assert allowed[0]["reasons"] == ["verified_exact_target"]


def test_invoice_action_creates_review_only_tally_draft(monkeypatch):
    calls = []

    async def create(org_id, user_id, role, conversation_id, payload):
        calls.append((org_id, user_id, role, conversation_id, payload))
        return {"draft_id": "d1", "source_message_ids": ["m1"]}

    monkeypatch.setitem(sys.modules, "services.tally_service", SimpleNamespace(
        create_tally_invoice_draft=create,
    ))
    case = {
        "case_id": "case", "org_id": "org", "conversation_id": "whatsapp_1", "quote_id": "q1",
        "action_source_message_ids": ["m1"],
    }
    result = asyncio.run(actions._invoice_draft(case, "bill karo", "wamid.1"))
    assert result["fields"] == {"invoice_draft_id": "d1", "invoice_source_message_ids": ["m1"]}
    assert calls[0][1:3] == ("ai-sales", "admin")
    assert calls[0][4]["idempotency_key"] == "case:wamid.1:create_invoice_draft"


def test_same_sales_event_executes_only_once(monkeypatch):
    database = DB()
    calls = []
    states = []

    async def create_quote(case, source_message_id):
        calls.append(source_message_id)
        return {"reply": "quote", "fields": {"quote_id": "q1"},
                "ready_stage": "stock_confirmation", "delivered_stage": "quote_sent"}

    async def set_state(**kwargs):
        states.append(kwargs)

    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    monkeypatch.setattr(actions, "_create_quote", create_quote)
    monkeypatch.setattr(actions, "set_sales_action_state", set_state)
    case = {"case_id": "case", "org_id": "org", "conversation_id": "whatsapp_1"}

    first = asyncio.run(actions.execute_sales_action(
        case=case, source_message_id="message", action_type="create_quote", customer_text="quote",
    ))
    second = asyncio.run(actions.execute_sales_action(
        case=case, source_message_id="message", action_type="create_quote", customer_text="quote",
    ))

    assert first == second
    assert calls == ["message"]
    assert database.jobs.docs[first["job_id"]]["attempts"] == 1
    assert states[-1]["status"] == "ready"


def test_processing_sales_event_does_not_create_a_temporary_reply(monkeypatch):
    database = DB()
    database.jobs.docs["case:message:create_quote"] = {
        "_id": "case:message:create_quote", "status": "processing",
    }
    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    result = asyncio.run(actions.execute_sales_action(
        case={"case_id": "case", "org_id": "org", "conversation_id": "whatsapp_1"},
        source_message_id="message", action_type="create_quote", customer_text="quote",
    ))
    assert result == {"pending": True, "job_id": "case:message:create_quote"}
    assert "reply" not in result


def test_legacy_action_without_a_saved_result_requires_review(monkeypatch):
    database = DB()
    calls = []

    async def create_quote(_case, source_message_id):
        calls.append(source_message_id)
        return {"reply": "unsafe"}

    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    monkeypatch.setattr(actions, "_create_quote", create_quote)
    result = asyncio.run(actions.execute_sales_action(
        case={"case_id": "case", "org_id": "org", "conversation_id": "whatsapp_1"},
        source_message_id="message", action_type="create_quote", customer_text="quote",
        recover_result_only=True,
    ))
    assert result == {"requires_review": True, "job_id": "case:message:create_quote"}
    assert calls == []


def test_stale_worker_cannot_overwrite_newer_action_result(monkeypatch):
    database = DB()
    winner = {"reply": "winner", "job_id": "case:message:create_quote"}

    async def stale_quote(_case, _source_message_id):
        job = database.jobs.docs["case:message:create_quote"]
        job.update({"status": "ready", "claim_id": "new-owner", "result": winner})
        return {"reply": "stale", "fields": {}, "ready_stage": None, "delivered_stage": None}

    async def set_state(**_kwargs):
        return None

    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    monkeypatch.setattr(actions, "_create_quote", stale_quote)
    monkeypatch.setattr(actions, "set_sales_action_state", set_state)
    result = asyncio.run(actions.execute_sales_action(
        case={"case_id": "case", "org_id": "org", "conversation_id": "whatsapp_1"},
        source_message_id="message", action_type="create_quote", customer_text="quote",
    ))
    assert result == winner
    assert database.jobs.docs["case:message:create_quote"]["result"] == winner


def test_blocked_action_result_is_replayed_without_reexecution(monkeypatch):
    database = DB()
    calls = []

    async def blocked_quote(_case, source_message_id):
        calls.append(source_message_id)
        raise actions.SalesActionBlocked("Need an exact product.")

    async def set_state(**_kwargs):
        return None

    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    monkeypatch.setattr(actions, "_create_quote", blocked_quote)
    monkeypatch.setattr(actions, "set_sales_action_state", set_state)
    case = {"case_id": "case", "org_id": "org", "conversation_id": "whatsapp_1"}
    first = asyncio.run(actions.execute_sales_action(
        case=case, source_message_id="message", action_type="create_quote", customer_text="quote",
    ))
    second = asyncio.run(actions.execute_sales_action(
        case=case, source_message_id="message", action_type="create_quote", customer_text="quote",
    ))
    assert first == second
    assert calls == ["message"]


def test_unsafe_block_requires_durable_webhook_review(monkeypatch):
    database = DB()

    async def blocked_quote(_case, _source_message_id):
        raise actions.SalesActionBlocked("Live price needs review.", escalate=True)

    async def set_state(**_kwargs):
        return None

    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    monkeypatch.setattr(actions, "_create_quote", blocked_quote)
    monkeypatch.setattr(actions, "set_sales_action_state", set_state)
    result = asyncio.run(actions.execute_sales_action(
        case={"case_id": "case", "org_id": "org", "conversation_id": "whatsapp_1"},
        source_message_id="message", action_type="create_quote", customer_text="quote",
    ))

    assert result["requires_review"] is True
    assert result["review_category"] == "sales_action_review_required"
    assert database.jobs.docs[result["job_id"]]["result"] == result


def test_delivered_quote_schedules_one_existing_followup(monkeypatch):
    database = DB()
    database.jobs.docs["job"] = {"_id": "job", "status": "ready"}
    scheduled = []

    async def set_state(**kwargs):
        pass

    async def schedule(*args, **kwargs):
        scheduled.append((args, kwargs))

    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    monkeypatch.setattr(actions, "set_sales_action_state", set_state)
    monkeypatch.setitem(sys.modules, "services.followup_service", SimpleNamespace(
        schedule_followup_message=schedule,
    ))
    action = {
        "job_id": "job", "case_id": "case", "conversation_id": "whatsapp_1",
        "source_message_id": "message", "action_type": "create_quote",
        "fields": {"quote_id": "quote"}, "delivered_stage": "quote_sent",
    }
    asyncio.run(actions.mark_sales_action_delivered("org", action))
    asyncio.run(actions.mark_sales_action_delivered("org", action))

    assert len(scheduled) == 1
    assert scheduled[0][1]["source"] == "quotation"
    assert scheduled[0][1]["quote_id"] == "quote"


def test_delivery_finalization_retries_after_case_update_failure(monkeypatch):
    database = DB()
    database.jobs.docs["job"] = {"_id": "job", "status": "ready"}
    attempts = []

    async def set_state(**_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    monkeypatch.setattr(actions, "set_sales_action_state", set_state)
    action = {
        "job_id": "job", "case_id": "case", "conversation_id": "whatsapp_1",
        "source_message_id": "message", "action_type": "request_stock_confirmation",
    }

    try:
        asyncio.run(actions.mark_sales_action_delivered("org", action))
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed delivery finalization was marked complete")
    assert database.jobs.docs["job"]["status"] == "ready"

    asyncio.run(actions.mark_sales_action_delivered("org", action))
    assert attempts == [True, True]
    assert database.jobs.docs["job"]["status"] == "delivered"
