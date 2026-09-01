from scripts.migrate_multi_whatsapp import build_plan, merge_conversation_documents, organization_waba_errors


def _connection(row_id, org_id, phone_id, wa_id="waba-1", connected_at="2026-01-01", **extra):
    return {
        "_id": row_id,
        "org_id": org_id,
        "phone_id": phone_id,
        "wa_id": wa_id,
        "connected_at": connected_at,
        "name": extra.pop("name", "Sales"),
        **extra,
    }


def test_plan_deduplicates_reconnects_and_assigns_two_slots():
    plan = build_plan([
        _connection("old", "org-1", "phone-1", connected_at="2026-01-01"),
        _connection("new", "org-1", "phone-1", connected_at="2026-02-01"),
        _connection("support", "org-1", "phone-2", connected_at="2026-03-01"),
    ])

    assert plan["errors"] == []
    org = plan["organizations"]["org-1"]
    assert org["delete_ids"] == ["old"]
    assert [(item["_id"], item["slot"]) for item in org["updates"]] == [("new", 1), ("support", 2)]
    assert all(item["phone_number_id"] == item["phone_id"] for item in org["updates"])
    assert all(item["waba_id"] == item["wa_id"] for item in org["updates"])
    assert len({item["label_key"] for item in org["updates"]}) == 2


def test_plan_backfills_legacy_identifier_aliases():
    plan = build_plan([{
        "_id": "legacy",
        "org_id": "org-1",
        "phone_number_id": "phone-1",
        "waba_id": "waba-1",
        "connected_at": "2026-01-01",
    }])
    assert plan["errors"] == []
    update = plan["organizations"]["org-1"]["updates"][0]
    assert update["phone_id"] == "phone-1"
    assert update["wa_id"] == "waba-1"


def test_plan_refuses_cross_org_ownership_and_allows_mixed_wabas():
    plan = build_plan([
        _connection("one", "org-1", "phone-1", "waba-1"),
        _connection("two", "org-1", "phone-2", "waba-2"),
        _connection("stolen", "org-2", "phone-1", "waba-1"),
    ])

    errors = "\n".join(plan["errors"])
    assert "multiple organizations" in errors
    assert "mixed WABAs" not in errors


def test_plan_prefers_active_reconnect_and_does_not_resurrect_inactive_rows():
    plan = build_plan([
        _connection("active", "org-1", "phone-1", connected_at="2026-01-01"),
        _connection("inactive-newer", "org-1", "phone-1", connected_at="2026-03-01", is_active=False),
        _connection("inactive-only", "org-2", "phone-2", is_active=False),
    ])

    assert [item["_id"] for item in plan["organizations"]["org-1"]["updates"]] == ["active"]
    assert plan["organizations"]["org-1"]["delete_ids"] == ["inactive-newer"]
    assert plan["organizations"]["org-2"]["updates"] == []
    assert plan["organizations"]["org-2"]["delete_ids"] == ["inactive-only"]


def test_plan_keeps_generated_labels_unique_when_defaults_are_already_used():
    plan = build_plan([
        _connection("one", "org-1", "phone-1", name="WhatsApp 2"),
        _connection("two", "org-1", "phone-2", name="WhatsApp 2"),
    ])

    labels = [item["label_key"] for item in plan["organizations"]["org-1"]["updates"]]
    assert len(labels) == len(set(labels)) == 2


def test_plan_accepts_fifty_numbers_and_rejects_fifty_one():
    fifty = [_connection(str(index), "org-1", f"phone-{index}") for index in range(50)]
    assert build_plan(fifty)["errors"] == []
    assert len(build_plan(fifty)["organizations"]["org-1"]["updates"]) == 50
    assert "51 distinct WhatsApp numbers" in "\n".join(
        build_plan([*fifty, _connection("50", "org-1", "phone-50")])["errors"]
    )


def test_duplicate_conversations_keep_newest_state_and_oldest_start():
    merged = merge_conversation_documents([
        {
            "_id": "old", "conversation_id": "legacy", "customer_id": "customer",
            "created_at": "2026-01-01", "last_message_timestamp": "2026-01-02",
            "message_counter": 5, "unread_count": 1, "customer_name": "Old",
        },
        {
            "_id": "new", "conversation_id": "sender-aware", "customer_id": "customer",
            "created_at": "2026-01-03", "last_message_timestamp": "2026-01-04",
            "message_counter": 3, "unread_count": 2, "customer_name": "Current",
        },
    ], "phone")
    assert merged["survivor_id"] == "new"
    assert merged["duplicate_ids"] == ["old"]
    assert merged["conversation_ids"] == ["legacy", "sender-aware"]
    assert merged["document"]["business_phone_id"] == "phone"
    assert merged["document"]["created_at"] == "2026-01-01"
    assert merged["document"]["message_counter"] == 5
    assert merged["document"]["unread_count"] == 2
    assert merged["document"]["customer_name"] == "Current"


def test_preflight_rejects_waba_owners():
    assert organization_waba_errors([
        {"org_id": "org-1", "wa_id": "waba"},
        {"org_id": "org-2", "wa_id": "waba"},
    ]) == ["WABA waba is assigned to multiple organizations: ['org-1', 'org-2']"]


if __name__ == "__main__":
    test_plan_deduplicates_reconnects_and_assigns_two_slots()
    test_plan_backfills_legacy_identifier_aliases()
    test_plan_refuses_cross_org_ownership_and_allows_mixed_wabas()
    test_plan_prefers_active_reconnect_and_does_not_resurrect_inactive_rows()
    test_plan_keeps_generated_labels_unique_when_defaults_are_already_used()
    test_plan_accepts_fifty_numbers_and_rejects_fifty_one()
    test_duplicate_conversations_keep_newest_state_and_oldest_start()
    test_preflight_rejects_waba_owners()
    print("Multi-number WhatsApp migration tests passed.")
