from scripts.migrate_multi_whatsapp import build_plan


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
    ], {"org-1"})

    assert plan["errors"] == []
    org = plan["organizations"]["org-1"]
    assert org["delete_ids"] == ["old"]
    assert [(item["_id"], item["slot"]) for item in org["updates"]] == [("new", 1), ("support", 2)]
    assert len({item["label_key"] for item in org["updates"]}) == 2


def test_plan_refuses_cross_org_ownership_mixed_wabas_and_unentitled_second_line():
    plan = build_plan([
        _connection("one", "org-1", "phone-1", "waba-1"),
        _connection("two", "org-1", "phone-2", "waba-2"),
        _connection("stolen", "org-2", "phone-1", "waba-1"),
    ], set())

    errors = "\n".join(plan["errors"])
    assert "multiple organizations" in errors
    assert "mixed WABAs" in errors
    assert "not allowlisted" in errors


def test_plan_prefers_active_reconnect_and_does_not_resurrect_inactive_rows():
    plan = build_plan([
        _connection("active", "org-1", "phone-1", connected_at="2026-01-01"),
        _connection("inactive-newer", "org-1", "phone-1", connected_at="2026-03-01", is_active=False),
        _connection("inactive-only", "org-2", "phone-2", is_active=False),
    ], set())

    assert [item["_id"] for item in plan["organizations"]["org-1"]["updates"]] == ["active"]
    assert plan["organizations"]["org-1"]["delete_ids"] == ["inactive-newer"]
    assert plan["organizations"]["org-2"]["updates"] == []
    assert plan["organizations"]["org-2"]["delete_ids"] == ["inactive-only"]


def test_plan_keeps_generated_labels_unique_when_defaults_are_already_used():
    plan = build_plan([
        _connection("one", "org-1", "phone-1", name="WhatsApp 2"),
        _connection("two", "org-1", "phone-2", name="WhatsApp 2"),
    ], {"org-1"})

    labels = [item["label_key"] for item in plan["organizations"]["org-1"]["updates"]]
    assert len(labels) == len(set(labels)) == 2


if __name__ == "__main__":
    test_plan_deduplicates_reconnects_and_assigns_two_slots()
    test_plan_refuses_cross_org_ownership_mixed_wabas_and_unentitled_second_line()
    test_plan_prefers_active_reconnect_and_does_not_resurrect_inactive_rows()
    test_plan_keeps_generated_labels_unique_when_defaults_are_already_used()
    print("Multi-number WhatsApp migration tests passed.")
