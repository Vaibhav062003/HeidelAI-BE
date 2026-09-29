"""Shared draft/publication rules for the two existing automation engines."""
from copy import deepcopy
from fastapi import HTTPException

WA_DRAFT_FIELDS = {
    "name", "description", "trigger_type", "trigger_messages", "welcome_message",
    "nodes", "connections", "introduction_message", "start_node_id", "ui_config",
    "trigger_from", "trigger_event", "priority", "tags",
}


def published_snapshot(flow):
    if "published_snapshot" in flow:
        return deepcopy(flow["published_snapshot"])
    fields = WA_DRAFT_FIELDS if flow.get("flow_type") == "wabot" or "trigger_messages" in flow else {"name", "flow_data"}
    return {key: deepcopy(flow[key]) for key in fields if key in flow}


def public_flow(flow):
    result = {key: value for key, value in flow.items() if key != "published_snapshot"}
    result["draft_version"] = flow.get("version", 1)
    result["published_version"] = flow.get("published_version")
    if result["published_version"] is None and flow.get("status") in {"active", "published"}:
        result["published_version"] = result["draft_version"]
    return result


def version_query(flow, expected=None):
    version = flow.get("version", 1)
    if expected is not None and expected != version:
        raise HTTPException(409, "This workflow changed. Reload before saving or publishing.")
    query = {"flow_id": flow["flow_id"], "org_id": flow["org_id"]}
    query["version"] = version if "version" in flow else {"$exists": False}
    return query


def draft_fields(flow, payload, allowed):
    fields = {key: deepcopy(value) for key, value in payload.items() if key in allowed}
    fields["version"] = flow.get("version", 1) + 1
    if flow.get("status") in {"active", "published"} and "published_snapshot" not in flow:
        fields["published_snapshot"] = published_snapshot(flow)
        fields["published_version"] = flow.get("version", 1)
    return fields
