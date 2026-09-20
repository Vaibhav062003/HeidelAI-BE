import ast
from pathlib import Path


wa_routes = Path("api/wa_routes.py").read_text()
webhook_routes = Path("api/webhook_routes.py").read_text()
wa_service = Path("services/wa_service.py").read_text()
lifespan = Path("core/lifespan.py").read_text()
cloudinary_service = Path("services/cloudinary_service.py").read_text()
scheduler = Path("core/scheduler.py").read_text()


def call_event_payload_keys(source: str) -> list[set[str]]:
    tree = ast.parse(source)
    named_payloads = {
        node.targets[0].id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Dict)
    }
    payloads = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "broadcast_main_ws":
            continue
        if len(node.args) < 4 or not isinstance(node.args[2], ast.Constant) or node.args[2].value != "whatsapp_call":
            continue
        payload = named_payloads.get(node.args[3].id) if isinstance(node.args[3], ast.Name) else node.args[3]
        assert isinstance(payload, ast.Dict)
        payloads.append({key.value for key in payload.keys if isinstance(key, ast.Constant)})
    return payloads


assert '{"org_id": user.org_id, "call_id": call_id}' in wa_routes
payloads = call_event_payload_keys(wa_routes) + call_event_payload_keys(webhook_routes)
assert len(payloads) == 5 and all({"id", "call_id"} <= keys for keys in payloads)
assert "len(sdp) > 100_000" in webhook_routes
assert '**safe_call' in webhook_routes
assert '[("org_id", 1), ("call_id", 1)]' in wa_service
assert "unique=True" in wa_service
assert "ensure_whatsapp_call_indexes" in lifespan
assert "except httpx.RequestError" in wa_routes
assert 'str(detail.get("code")) != "138000"' in wa_routes
assert '"POST", "settings"' in wa_routes
assert '"srtp_key_exchange_protocol": "DTLS"' in wa_routes
assert 'error_data.get("details")' in wa_routes
assert "window_end.tzinfo is None" in wa_routes
assert '"errors": safe_call.get("errors") or value.get("errors")' in webhook_routes
assert 'call.get("status") or call.get("event")' in webhook_routes
assert '@router.get("/calls")' in wa_routes
assert '@router.get("/calls/ice-config")' in wa_routes
assert 'hashlib.sha1' in wa_routes and 'timestamp()) + 3600' in wa_routes
assert '@router.get("/calls/{call_id}")' in wa_routes
assert '@router.post("/calls/{call_id}/recording")' in wa_routes
assert '@router.get("/calls/{call_id}/recording")' in wa_routes
assert '@router.post("/calls/{call_id}/recording-sessions")' in wa_routes
assert '/parts/{sequence}/signature' in wa_routes
assert '/parts/{sequence}/complete' in wa_routes
assert '/recording-sessions/{session_id}/finalize' in wa_routes
assert 'CALL_RECORDING_MAX_BYTES' in wa_routes
assert 'CALL_RECORDING_MAX_DURATION_SECONDS' in wa_routes
assert 'whatsapp_call_recording_download_url' in wa_routes
assert 'type="private"' in cloudinary_service
assert 'CALL_RECORDING_RETENTION_DAYS = 15' in cloudinary_service
assert 'CALL_RECORDING_ORG_QUOTA_BYTES = 1024 * 1024 * 1024' in cloudinary_service
assert '"overwrite": "false"' in cloudinary_service
assert '"$inc": {"confirmed_bytes": part["bytes"]}' in wa_routes
assert 'RECORDING_CONSENT_METHOD = "verbal_agent_attestation"' in wa_routes
assert '"lease_token": token' in cloudinary_service
assert '"delete_lease_until": now + timedelta(minutes=5)' in cloudinary_service
assert 'recording_quota_evictions' in cloudinary_service
assert '"event": "recording_downloaded"' in wa_routes
assert '"event": "consent_withdrawn"' in wa_routes
assert '"recording.status": {"$in": ["ready", "expired"]}' in cloudinary_service
assert 'scheduler.add_job(process_call_recording_jobs, "interval", seconds=10' in scheduler
assert 'scheduler.add_job(cleanup_expired_call_recordings, "interval", minutes=5' in scheduler
assert 'cleanup_orphaned_call_recording_parts' in scheduler
assert 'CALL_HISTORY_PROJECTION' in wa_routes
assert '"state": {"$nin": list(STORED_TERMINAL_CALL_STATES)}' in wa_routes
assert "provider_lifecycle_filter" in webhook_routes

print("WhatsApp call backend contract check passed.")
