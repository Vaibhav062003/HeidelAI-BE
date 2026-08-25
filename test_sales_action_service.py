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
        if not document or document.get("status") == query.get("status", {}).get("$ne"):
            return None
        if "$or" in query and document.get("status") not in {"blocked", "failed"}:
            return None
        old = dict(document)
        document.update(update.get("$set", {}))
        for field, amount in update.get("$inc", {}).items():
            document[field] = document.get(field, 0) + amount
        return document if return_document is not None else old


class DB:
    def __init__(self):
        self.jobs = Jobs()

    def __getitem__(self, name):
        return self.jobs


class PaymentLinks:
    def __init__(self, document):
        self.document = document

    async def find_one(self, query):
        return self.document if query.get("link_id") == self.document.get("link_id") else None


class PaymentDB(DB):
    def __init__(self, document):
        super().__init__()
        self.sales_payment_links = PaymentLinks(document)


class ProductCollection:
    def __init__(self, document):
        self.document = document

    async def find_one(self, query):
        return self.document


class ProductDB:
    def __init__(self):
        self.collections = {
            "product_catalogue": ProductCollection({"product_id": "P1", "model_no": "ABC"}),
            "products_catalogue": ProductCollection({"unique_identifier": "ABC", "stock": 4,
                                                       "source": "google_sheet"}),
        }

    def __getitem__(self, name):
        return self.collections[name]


def test_stock_count_requires_a_real_number():
    assert actions._stock_count({"stock": 4}) == 4
    assert actions._stock_count({"stock": "1,000"}) == 1000
    assert actions._stock_count({"in_stock": True}) is None


def test_v5_selection_resolves_to_the_live_sheet_product(monkeypatch):
    monkeypatch.setattr(actions, "get_async_mongo_db", ProductDB)
    product = asyncio.run(actions._product("catalogue", "P1"))
    assert product["unique_identifier"] == "ABC"
    assert product["source"] == "google_sheet"


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


def test_only_a_full_matching_payment_marks_the_deal_won(monkeypatch):
    database = PaymentDB({
        "link_id": "sale_1", "org_id": "org", "case_id": "case",
        "conversation_id": "whatsapp_1", "amount": 1180,
    })
    states = []
    aborted = []

    async def set_state(**kwargs):
        states.append(kwargs)

    async def abort(org_id, conversation_id):
        aborted.append((org_id, conversation_id))

    monkeypatch.setattr(actions, "get_async_mongo_db", lambda: database)
    monkeypatch.setattr(actions, "set_sales_action_state", set_state)
    monkeypatch.setitem(sys.modules, "services.followup_service", SimpleNamespace(
        abort_scheduled_message=abort,
    ))
    partial = {"data": {"link_id": "sale_1", "link_status": "PAID",
                        "link_amount": 1180, "link_amount_paid": 1000}}
    full = {"data": {"link_id": "sale_1", "link_status": "PAID",
                     "link_amount": 1180, "link_amount_paid": 1180}}

    assert asyncio.run(actions.apply_sales_payment_webhook(partial)) is False
    assert asyncio.run(actions.apply_sales_payment_webhook(full)) is True
    assert len(states) == 1 and states[0]["stage"] == "won"
    assert aborted == [("org", "whatsapp_1")]
