import ast
import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional


def _search_helpers():
    tree = ast.parse(Path("services/utils.py").read_text())
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"phone_search_parts", "name_search_prefixes"}
    ]
    namespace = {}
    module = ast.Module(body=[ast.Import(names=[ast.alias("re")]), *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "phone-search", "exec"), namespace)
    return namespace["phone_search_parts"], namespace["name_search_prefixes"]


def test_phone_search_parts_are_country_agnostic():
    phone_search_parts, name_search_prefixes = _search_helpers()
    digits, suffixes = phone_search_parts("+1 (415) 555-0100")
    assert digits == "14155550100"
    assert "4155550100" in suffixes
    assert not digits.startswith("91")

    digits, suffixes = phone_search_parts("+44 20 7946 0958")
    assert digits == "442079460958"
    assert "2079460958" in suffixes

    digits, suffixes = phone_search_parts("+65 6123 4567")
    assert digits == "6561234567"
    assert suffixes[-1] == digits
    prefixes = name_search_prefixes("Samar Tanwar")
    assert {"sam", "samar", "tan", "tanwar"} <= set(prefixes)
    assert "hayaa" in name_search_prefixes("k_i_n_g_hayaa___")

    service_tree = ast.parse(Path("services/common_service.py").read_text())
    context = next(node for node in service_tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "fetch_message_context")
    assert "raw_data" not in {node.value for node in ast.walk(context) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    lookup = next(node for node in service_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_lookup_scope")
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[lookup], type_ignores=[])), "lookup-scope", "exec"), namespace)
    assert namespace["_lookup_scope"]({"$and": [{"platform": "whatsapp"}]}) == {
        "$and": [{"conversation.platform": "whatsapp"}]
    }

    contact_search = next(node for node in service_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_contact_search_condition")
    namespace = {"re": re, "Dict": dict}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[contact_search], type_ignores=[])), "contact-search", "exec"), namespace)
    assert namespace["_contact_search_condition"]("Patil", False, "", ["patil"]) == {
        "$and": [{"$or": [
            {field: {"$regex": "patil", "$options": "i"}}
            for field in ("full_name", "username", "company", "tally.legal_name")
        ]}]
    }
    phone_condition = namespace["_contact_search_condition"]("987654", True, "987654", [])
    assert phone_condition["$or"][0]["phone_number"]["$regex"] == "987654$"

    search_source = ast.unparse(next(
        node for node in service_tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "search_conversations"
    ))
    assert 'contacts_{org_id}' in search_source
    assert "contact_conversation_ids" in search_source
    assert "conversation_access_condition" in search_source
    assert "item['customer_name'] = contact_name" in search_source

    wa_source = Path("services/wa_service.py").read_text()
    assert '"phone_digits": phone_digits' in wa_source
    assert '"phone_search_suffixes": phone_search_suffixes' in wa_source
    assert '"customer_name_prefixes"' in wa_source
    assert '"customer_name_prefixes"' in Path("services/contacts_service.py").read_text()


def test_queue_window_filters():
    # Execute the real service functions without a running MongoDB or app server.
    names = {
        "_queue_filter_conditions", "_priority_condition", "_lookup_scope", "_contact_search_condition",
        "_transferred_condition", "_escalated_condition", "_search_snippet", "search_conversations", "fetch_conversations_new",
    }
    tree = ast.parse(Path("services/common_service.py").read_text())
    namespace = {**globals(), "phone_search_parts": _search_helpers()[0]}
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])), "queue-services", "exec"), namespace)
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    conditions = namespace["_queue_filter_conditions"]
    filters = {"reply_window_under_minutes": 120, "ad_pricing_active": True, "ad_pricing_under_minutes": 360}

    def matches(doc, query):
        for field, condition in query.items():
            if field == "$and":
                if not all(matches(doc, part) for part in condition):
                    return False
                continue
            if field == "$or":
                if not any(matches(doc, part) for part in condition):
                    return False
                continue
            value = doc
            for key in field.split("."):
                value = value.get(key) if isinstance(value, dict) else None
            if not isinstance(condition, dict) or not any(key.startswith("$") for key in condition):
                if value != condition and not (isinstance(value, list) and condition in value):
                    return False
                continue
            for operator, target in condition.items():
                if operator == "$type" and not isinstance(value, datetime): return False
                if operator == "$gt" and (value is None or value <= target): return False
                if operator == "$lt" and (value is None or value >= target): return False
                if operator == "$ne" and value == target: return False
                if operator == "$in" and value not in target: return False
                if operator == "$nin" and value in target: return False
                if operator == "$all" and not all(item in (value or []) for item in target): return False
                if operator == "$exists" and (value is not None) != target: return False
                if operator == "$elemMatch" and not any(matches(item, target) for item in value or []): return False
                if operator == "$regex" and not re.search(target, str(value or ""), re.I if condition.get("$options") == "i" else 0): return False
                assert operator in {"$type", "$gt", "$lt", "$ne", "$in", "$nin", "$all", "$exists", "$elemMatch", "$regex", "$options"}, operator
        return True

    window_query = {"$and": conditions("all", None, None, None, None, None, now=now, **filters)}
    base = {"platform": "whatsapp", "reply_window_ends_at": now - timedelta(minutes=1), "free_entry_window_ends_at": now + timedelta(hours=5)}
    for minutes, expected in [(-60, True), (119, True), (120, False), (121, False)]:
        assert matches({**base, "reply_window_ends_at": now + timedelta(minutes=minutes)}, window_query) == expected
    for minutes, expected in [(-1, False), (0, False), (1, True), (359, True), (360, False), (361, False)]:
        assert matches({**base, "free_entry_window_ends_at": now + timedelta(minutes=minutes)}, window_query) == expected
    for field in ("reply_window_ends_at", "free_entry_window_ends_at"):
        for value in (None, "invalid", now.isoformat()):
            assert not matches({**base, field: value}, window_query)
        assert not matches({key: value for key, value in base.items() if key != field}, window_query)
    assert not matches({**base, "platform": "instagram"}, window_query)
    assert conditions("all", None, None, None, None, None, now=now) == []
    active_query = {"$and": conditions("all", None, None, None, None, None, now=now, ad_pricing_active=True)}
    assert matches({**base, "free_entry_window_ends_at": now + timedelta(hours=50)}, active_query)

    class Cursor:
        def __init__(self, rows): self.rows = list(rows)
        def sort(self, key, direction=None):
            fields = key if isinstance(key, list) else [(key, direction)]
            for field, order in reversed(fields):
                self.rows.sort(key=lambda doc: doc.get(field, now), reverse=order < 0)
            return self
        def skip(self, count): self.rows = self.rows[count:]; return self
        def limit(self, count): self.rows = self.rows[:count]; return self
        async def to_list(self, length): return self.rows if length is None else self.rows[:length]

    class Collection:
        def __init__(self, rows): self.rows = rows; self.pipelines = []
        def find(self, query, projection): return Cursor(doc for doc in self.rows if matches(doc, query))
        async def count_documents(self, query): return sum(matches(doc, query) for doc in self.rows)
        def aggregate(self, pipeline, **kwargs):
            self.pipelines.append(pipeline)
            if "$facet" in pipeline[0]:
                return Cursor([{key: [{"value": sum(matches(doc, facet[0]["$match"]) for doc in self.rows)}] for key, facet in pipeline[0]["$facet"].items()}])
            return Cursor([])

    good = [{**base, "conversation_id": f"contact-{index}", "customer_id": "123456", "customer_name": "pricing", "customer_name_prefixes": ["pricing"],
             "last_message_timestamp": now - timedelta(minutes=index), "business_phone_id": "phone-a", "assigned_agent_id": "agent-a", "status": "open", "priority": "high", "visible_to": "agent-a"}
            for index in range(71)]
    excluded = [{**good[0], "conversation_id": f"expired-{index}", "free_entry_window_ends_at": now, "last_message_timestamp": now + timedelta(minutes=index)} for index in range(60)]
    excluded += [{**good[0], "conversation_id": f"scope-{index}", **changes} for index, changes in enumerate([
        {"business_phone_id": "phone-b"}, {"assigned_agent_id": "agent-b"}, {"status": "closed"}, {"priority": "low"}, {"visible_to": "other-agent"},
    ])]
    conversations = Collection(excluded + good)
    messages = Collection([])
    db = {"conversations_org": conversations, "messages_org": messages, "contacts_org": Collection([])}

    async def access(*args): return {"visible_to": "agent-a"}
    async def phone_scope(org_id, phone_id): return {"business_phone_id": phone_id}
    async def no_op(*args): pass
    namespace.update(get_async_mongo_db=lambda: db, conversation_access_condition=access, _business_phone_scope=phone_scope,
                     ensure_conversation_search_indexes=no_op, serialize_mongo=lambda value: dict(value), logger=SimpleNamespace(info=lambda *args: None))
    namespace["_queue_filter_conditions"] = lambda *args, **kwargs: conditions(*args, **kwargs, now=now)
    args = dict(org_id="org", business_phone_id="phone-a", assigned_agent_id="agent-a", status_filter="open", priority="high", **filters)

    async def check_pages():
        first = await namespace["fetch_conversations_new"](**args, include_meta=True)
        second = await namespace["fetch_conversations_new"](**args, skip=50)
        assert first["total"] == 71 and first["has_more"]
        assert [row["id"] for row in first["conversations"] + second] == [row["conversation_id"] for row in good]
        assert first["counts"]["all"] > first["total"], "tab counts keep their existing unfiltered scope"
        for query in ("123456", "pricing"):
            first = await namespace["search_conversations"](**args, query=query, limit=50)
            second = await namespace["search_conversations"](**args, query=query, skip=50, limit=50)
            assert first["has_more"] and not second["has_more"]
            assert [row["id"] for row in first["conversations"] + second["conversations"]] == [row["conversation_id"] for row in good]
        lookup_query = next(stage["$match"] for stage in messages.pipelines[-1] if "$match" in stage and "$and" in stage["$match"])
        assert matches({"conversation": good[0]}, lookup_query)
        assert not matches({"conversation": excluded[0]}, lookup_query), "message search must apply timer filters before limiting results"

    asyncio.run(check_pages())
    route_tree = ast.parse(Path("api/common_routes.py").read_text())
    for route in [node for node in route_tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in {"get_conversations", "search_conversation_queue"}]:
        defaults = dict(zip([arg.arg for arg in route.args.args][-len(route.args.defaults):], route.args.defaults))
        for key in ("reply_window_under_minutes", "ad_pricing_under_minutes"):
            assert {kw.arg: ast.literal_eval(kw.value) for kw in defaults[key].keywords} == {"ge": 1, "le": 4320}
        forwarded = {kw.arg for node in ast.walk(route) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"fetch_conversations_new", "search_conversations"} for kw in node.keywords}
        assert filters.keys() <= forwarded


if __name__ == "__main__":
    test_phone_search_parts_are_country_agnostic()
    test_queue_window_filters()
    print("Conversation search, time-window and pagination checks passed.")
