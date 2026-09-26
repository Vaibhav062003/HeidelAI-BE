"""Advertise and validate what the existing executors can actually run."""

TRIGGER_ALIASES = {
    "instagram_message": "instagram_dm_received",
    "story_reply": "instagram_story_reply",
}
ACTION_TYPES = {"reply_to_comment", "send_dm", "add_tag", "remove_tag", "set_field", "api"}
GRAPH_STEPS = [
    "content_text", "action_reply_comment", "action_send_dm", "action_tag",
    "action_remove_tag", "action_field", "action_api", "logic_condition", "logic_delay", "logic_stop",
]
NODES_BY_TRIGGER = {
    "trigger_whatsapp_message": [],  # The dedicated WhatsApp editor owns this engine.
    "trigger_instagram_message": [step for step in GRAPH_STEPS if step not in {"action_reply_comment", "action_send_dm"}],
    "trigger_comment": [step for step in GRAPH_STEPS if step not in {"content_text", "action_tag", "action_remove_tag", "action_field"}],
    "trigger_story_reply": [step for step in GRAPH_STEPS if step not in {"action_reply_comment", "action_send_dm"}],
    "trigger_google_sheet": ["content_whatsapp_message"],
    "trigger_whatsapp_followup": ["logic_delay", "content_whatsapp_message"],
}
AUTOMATION_NODE_TYPES = set(NODES_BY_TRIGGER) | {step for steps in NODES_BY_TRIGGER.values() for step in steps}


def automation_capabilities():
    return {
        "node_types": sorted(AUTOMATION_NODE_TYPES),
        "nodes_by_trigger": NODES_BY_TRIGGER,
        "features": {"email_accounts": False, "preview_test": False, "live_test": False},
    }


def normalize_trigger(trigger_type):
    return TRIGGER_ALIASES.get(trigger_type, trigger_type)


def validate_executable_flow(data):
    nodes, connections, triggers = data.get("nodes", {}), data.get("connections", []), data.get("triggers", [])
    if len(triggers) != 1:
        raise ValueError("A runnable workflow requires exactly one trigger.")
    trigger = triggers[0]
    kind = normalize_trigger(trigger.get("type"))
    if kind not in {"instagram_dm_received", "instagram_comment", "instagram_story_reply", "google_sheet", "whatsapp_followup"}:
        raise ValueError(f"Trigger {kind} is not available yet.")
    start = trigger.get("start_node_id")
    if start not in nodes:
        raise ValueError("The trigger must connect to an executable step.")
    for node_id, node in nodes.items():
        node_type, config = node.get("type"), node.get("config", {})
        if node_type not in {"trigger", "message", "action", "condition", "smart_delay", "stop", "randomizer"}:
            raise ValueError(f"Step {node_type} is not available yet.")
        if node_type == "action" and config.get("action_type") not in ACTION_TYPES:
            raise ValueError(f"Action {config.get('action_type')} is not available yet.")
        if node_type == "action" and config.get("action_type") in {"reply_to_comment", "send_dm"} and kind != "instagram_comment":
            raise ValueError("Comment replies and private replies require a comment trigger.")
        if kind == "instagram_comment" and (node_type == "message" or config.get("action_type") in {"add_tag", "remove_tag", "set_field"}):
            raise ValueError("Comment triggers support comment replies and private replies. Contact and conversation steps require an incoming message.")
        if node_type == "message":
            content = config.get("content") or node.get("content", {})
            if not content.get("text") and not content.get("template_name") and not content.get("media_url"):
                raise ValueError("Message steps must contain a message.")
        if node_type == "condition":
            if not config.get("variable") or config.get("operator") not in {"equals", "not_equals", "contains", "not_contains", "includes", "not_includes", "greater_than", "less_than"}:
                raise ValueError("Complete the condition variable and operator.")
            handles = {edge.get("source_handle") or edge.get("sourceHandle") for edge in connections if edge.get("source") == node_id}
            if handles != {"true", "false"}:
                raise ValueError("Conditions require True and False connections.")
    visiting, visited = set(), set()

    def walk(node_id):
        if node_id in visiting:
            raise ValueError("Workflow connections cannot contain a cycle.")
        if node_id in visited:
            return
        if node_id not in nodes:
            raise ValueError("A workflow connection points to a missing step.")
        visiting.add(node_id)
        for edge in connections:
            if edge.get("source") == node_id:
                walk(edge.get("target"))
        visiting.remove(node_id)
        visited.add(node_id)

    walk(start)
    runtime = {key: value for key, value in nodes.items() if value.get("type") != "trigger"}
    if set(runtime) - visited:
        raise ValueError("Every executable step must connect to the trigger.")
    if kind in {"google_sheet", "whatsapp_followup"}:
        sequence, current = [], start
        while current:
            sequence.append(nodes[current].get("type"))
            children = [edge["target"] for edge in connections if edge.get("source") == current]
            if len(children) > 1:
                raise ValueError("This scheduler supports a single message path.")
            current = children[0] if children else None
        required = ["message"] if kind == "google_sheet" else ["smart_delay", "message"]
        if sequence != required:
            raise ValueError("Sheets support one message; follow-ups support Delay → Message.")
        if kind == "whatsapp_followup" and not trigger.get("config", {}).get("conversation_id"):
            raise ValueError("Select a conversation for this follow-up.")
    return kind
