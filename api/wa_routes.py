# Built-in modules
import uuid
import asyncio
import httpx
import requests
from typing import Optional
from datetime import datetime, timezone, timedelta

# Third-party modules
import cloudinary
from loguru import logger
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure
from fastapi import (
    Form,
    File,
    Depends,
    Request,
    Query,
    APIRouter,
    UploadFile,
    HTTPException,
    BackgroundTasks
)

# Internal modules
from core.limiter import limiter
from core.services import services
from services.utils import serialize_mongo
from schemas.models import WhatsappAuthRequest, WhatsappConnectionLabelRequest, WhatsappDisconnectRequest, MessageRole, Template, BulkMessageRequest
from services.wa_service import broadcast_whatsapp_message, finalize_whatsapp_revocation, store_whatsapp_message, send_whatsapp_message, get_templates, create_templates, create_bulk_job, get_broadcast_suppressed_phone_keys, run_bulk_send_job, resolve_outbound_whatsapp_connection
from services.platforms.whatsapp_service import WhatsAppService
from services.cloudinary_service import check_cloudinary_storage_limit
from database import get_async_mongo_db, get_mongo_db
from auth.dependencies import CurrentUser
from auth.auth_utils import admin_required, require_auth
from config.settings import (
    APP_ID,
    APP_SECRET,
    ACCESS_TOKEN,
    BUSINESS_ID,
    VERSION,
    WA_VERSION,
    CLOUDINARY_CLOUD_NAME,
    CLOUDINARY_API_KEY,
    CLOUDINARY_API_SECRET,
    LEE_DEALER_ORG_ID
)
from services.websocket_service import broadcast_main_ws
from services.ai_policy_service import set_conversation_ai_state
from services.entitlement_service import release_capacity, reserve_capacity
from services.work_team_service import conversation_access_condition, require_conversation_access
from services.whatsapp_call_history import (
    CALL_HISTORY_PROJECTION,
    STORED_TERMINAL_CALL_STATES,
    canonical_call_state,
    decode_call_cursor,
    encode_call_cursor,
    meta_datetime,
)
from services.whatsapp_connection_service import (
    choose_connection_slot,
    connection_label_key,
    normalize_connection_label,
    public_connection,
    public_connections,
    public_historical_connections,
    select_exact_phone_number,
    validate_active_connection_slots,
    whatsapp_connection_limit,
)


cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

whatsapp_service = WhatsAppService()

db = get_mongo_db()

router = APIRouter(prefix="/api/whatsapp", tags=["WhatsApp"])


def _whatsapp_schema_ready(org_id: str) -> bool:
    organization = db.organizations.find_one({"org_id": org_id}, {"whatsapp_connection_schema_version": 1}) or {}
    return organization.get("whatsapp_connection_schema_version") == 2


def _whatsapp_reconnect_identity(phone_id: str, waba_id: str) -> dict:
    identities = [{"wa_id": waba_id}, {"waba_id": waba_id}]
    if phone_id:
        identities.extend(({"phone_id": phone_id}, {"phone_number_id": phone_id}))
    return {"$or": identities}


async def _fence_whatsapp_reconnect_cleanup(org_id: str, phone_id: str, waba_id: str) -> None:
    pending = db.whatsapp_connections.find_one({
        "org_id": org_id,
        **_whatsapp_reconnect_identity(phone_id, waba_id),
        "is_active": False,
        "provider_revoke_status": {"$in": ["pending", "failed", "processing"]},
    })
    if not pending:
        return
    cleanup_phone_id = str(pending.get("phone_id") or pending.get("phone_number_id") or "")
    cleanup = await finalize_whatsapp_revocation(org_id, cleanup_phone_id)
    if cleanup.get("provider_cleanup") not in {"complete", "not_needed"}:
        raise HTTPException(status_code=409, detail="WhatsApp disconnect cleanup is still finishing. Try again shortly.")


def _claim_whatsapp_waba_owner(org_id: str, waba_id: str) -> None:
    """Atomically reserve a WABA for one tenant until its last line is disconnected."""
    try:
        db.whatsapp_waba_owners.update_one(
            {"_id": waba_id, "$or": [{"org_id": org_id}, {"org_id": {"$exists": False}}]},
            {
                "$set": {"org_id": org_id, "updated_at": datetime.now(timezone.utc)},
                "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
            },
            upsert=True,
        )
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="This WhatsApp Business Account is already owned by another organization",
        ) from exc
def _initialize_whatsapp_schema(org_id: str) -> None:
    db.whatsapp_connections.create_index(
        "phone_id", unique=True,
        partialFilterExpression={"phone_id": {"$type": "string"}, "is_active": True},
        name="whatsapp_phone_unique",
    )
    db.whatsapp_connections.create_index(
        [("org_id", 1), ("slot", 1)], unique=True,
        partialFilterExpression={"org_id": {"$type": "string"}, "slot": {"$type": "number"}, "is_active": True},
        name="whatsapp_org_slot_unique",
    )
    db.whatsapp_connections.create_index(
        [("org_id", 1), ("label_key", 1)], unique=True,
        partialFilterExpression={"org_id": {"$type": "string"}, "label_key": {"$type": "string"}, "is_active": True},
        name="whatsapp_org_label_unique",
    )
    db[f"conversations_{org_id}"].create_index(
        [("platform", 1), ("business_phone_id", 1), ("customer_id", 1)], unique=True,
        partialFilterExpression={
            "platform": "whatsapp",
            "business_phone_id": {"$type": "string"},
            "customer_id": {"$type": "string"},
        },
        name="whatsapp_phone_customer_unique",
    )
    required = {"whatsapp_phone_unique", "whatsapp_org_slot_unique", "whatsapp_org_label_unique"}
    if not required.issubset(db.whatsapp_connections.index_information()):
        raise RuntimeError("WhatsApp connection indexes could not be verified")
    if "whatsapp_phone_customer_unique" not in db[f"conversations_{org_id}"].index_information():
        raise RuntimeError("WhatsApp conversation index could not be verified")
    db.organizations.update_one({"org_id": org_id}, {"$set": {"whatsapp_connection_schema_version": 2}})


async def _fetch_meta_details_resource(
    client,
    url: str,
    headers: dict,
    params: dict,
    label: str,
    org_id: str,
    warnings: list,
    *,
    required: bool = False,
) -> dict:
    try:
        response = await client.get(url, headers=headers, params=params)
        if response.is_success:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        logger.warning("WhatsApp details {} request failed for org {}: status={}", label, org_id, response.status_code)
        if required and (response.status_code in {401, 403} or error.get("code") == 190):
            raise HTTPException(status_code=409, detail="Meta access expired. Reconnect this WhatsApp number.")
        if required:
            raise HTTPException(status_code=502, detail="Meta could not load this WhatsApp account. Try again.")
    except httpx.TimeoutException as exc:
        if required:
            raise HTTPException(status_code=504, detail="Meta took too long to load this WhatsApp account. Try again.") from exc
        logger.warning("WhatsApp details {} request timed out for org {}", label, org_id)
    except (httpx.RequestError, ValueError) as exc:
        if required:
            raise HTTPException(status_code=502, detail="Meta could not be reached. Try again.") from exc
        logger.warning("WhatsApp details {} request failed for org {} ({})", label, org_id, type(exc).__name__)
    warnings.append(f"{label} unavailable")
    return {}


def _find_owned_whatsapp_connection(
    org_id: str,
    *,
    connection_id: str | None = None,
    phone_number_id: str | None = None,
    include_inactive: bool = False,
) -> dict | None:
    query: dict = {"org_id": org_id}
    if not include_inactive:
        query["is_active"] = {"$ne": False}
    if phone_number_id:
        value = str(phone_number_id).strip()
        query["$or"] = [{"phone_id": value}, {"phone_number_id": value}]
        return db.whatsapp_connections.find_one(query)
    if connection_id:
        value = str(connection_id).strip()
        query["$or"] = [{"user_id": value}, {"phone_id": value}, {"phone_number_id": value}]
        return db.whatsapp_connections.find_one(query)
    return db.whatsapp_connections.find_one({**query, "slot": 1}) or db.whatsapp_connections.find_one(query)


def _calling_context(org_id: str, conversation_id: str | None = None, call_id: str | None = None):
    if call_id:
        call = db.whatsapp_calls.find_one({"call_id": call_id, "org_id": org_id})
        if not call:
            raise HTTPException(status_code=404, detail="This WhatsApp call is no longer available")
        wa_id = call.get("wa_id")
        business_phone_id = call.get("business_phone_id") or call.get("phone_id")
        conversation = None
    else:
        conversation = db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id})
        if not conversation or conversation.get("platform") != "whatsapp":
            raise HTTPException(status_code=404, detail="WhatsApp conversation not found")
        wa_id = conversation.get("whatsapp_id") or (db.organizations.find_one({"org_id": org_id}, {"wa_id": 1}) or {}).get("wa_id")
        business_phone_id = conversation.get("business_phone_id")

    connection = resolve_outbound_whatsapp_connection(
        org_id,
        wa_id,
        business_phone_id,
    )
    if not connection or not connection.get("phone_id") or not connection.get("access_token"):
        raise HTTPException(status_code=409, detail="Connect a WhatsApp Cloud API number before using calls")

    phone = None
    if conversation:
        raw_phone = str(conversation.get("customer_id") or conversation_id.removeprefix("whatsapp_"))
        phone = "".join(character for character in raw_phone if character.isdigit())
        if len(phone) < 7:
            raise HTTPException(status_code=422, detail="This contact does not have a callable WhatsApp number")
    return connection, conversation, phone


async def _meta_call_request(connection: dict, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None):
    version = WA_VERSION or VERSION or "v24.0"
    url = f"https://graph.facebook.com/{version}/{connection['phone_id']}/{path}"
    headers = {"Authorization": f"Bearer {connection['access_token']}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, headers=headers, json=json_body, params=params)
    except httpx.RequestError as error:
        logger.error("WhatsApp Calling API request failed: {} {} ({})", method, path, type(error).__name__)
        raise HTTPException(
            status_code=502,
            detail={"message": "Meta's Calling API could not be reached. Try again.", "code": "meta_network_error"},
        ) from error
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.is_error:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        if not isinstance(error, dict):
            error = {}
        error_data = error.get("error_data", {}) if isinstance(error, dict) else {}
        if not isinstance(error_data, dict):
            error_data = {}
        code = error.get("code")
        subcode = error.get("error_subcode")
        trace_id = error.get("fbtrace_id")
        logger.error(
            "WhatsApp Calling API rejected {} {}: status={} code={} subcode={} trace_id={}",
            method, path, response.status_code, code, subcode, trace_id,
        )
        raise HTTPException(
            status_code=502 if response.status_code >= 500 else 400,
            detail={
                "message": error_data.get("details") or error.get("message") or "Meta could not complete the WhatsApp call request",
                "code": code,
                "subcode": subcode,
                "trace_id": trace_id,
            },
        )
    return data


async def _enable_whatsapp_calling(connection: dict):
    return await _meta_call_request(connection, "POST", "settings", json_body={
        "calling": {
            "status": "ENABLED",
            "callback_permission_status": "ENABLED",
            "srtp_key_exchange_protocol": "DTLS",
        },
    })


def _validated_call_session(body: dict, expected_type: str):
    session = body.get("session")
    if not isinstance(session, dict) or session.get("sdp_type") != expected_type or not isinstance(session.get("sdp"), str):
        raise HTTPException(status_code=422, detail=f"A WebRTC SDP {expected_type} is required")
    if len(session["sdp"]) > 100_000:
        raise HTTPException(status_code=413, detail="The WebRTC session description is too large")
    return session


@router.get("/conversations/{conversation_id}/call-permission")
@limiter.limit("30/minute")
async def get_call_permission(request: Request, conversation_id: str, user: CurrentUser):
    await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)
    connection, _, phone = _calling_context(user.org_id, conversation_id=conversation_id)
    try:
        return await _meta_call_request(connection, "GET", "call_permissions", params={"user_wa_id": phone})
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        if str(detail.get("code")) != "138000":
            raise
        await _enable_whatsapp_calling(connection)
        return await _meta_call_request(connection, "GET", "call_permissions", params={"user_wa_id": phone})


@router.post("/conversations/{conversation_id}/call-permission")
@limiter.limit("5/minute")
async def request_call_permission(request: Request, conversation_id: str, user: CurrentUser):
    await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)
    connection, conversation, phone = _calling_context(user.org_id, conversation_id=conversation_id)
    window_end = conversation.get("reply_window_ends_at")
    if isinstance(window_end, datetime) and window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    if isinstance(window_end, datetime) and window_end <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="The reply window is closed. Send an approved call-permission template first.")
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "call_permission_request",
            "action": {"name": "call_permission_request"},
            "body": {"text": "Please allow us to call you on WhatsApp so we can help with your enquiry."},
        },
    }
    result = await _meta_call_request(connection, "POST", "messages", json_body=body)
    content = body["interactive"]["body"]["text"]
    message_id = ((result.get("messages") or [{}])[0]).get("id")
    db[f"conversations_{user.org_id}"].update_one(
        {"conversation_id": conversation_id},
        {"$set": {"last_message": f"{user.full_name or 'Agent'}: {content}", "last_sender": "agent", "last_message_timestamp": datetime.now(timezone.utc)}},
    )
    await store_whatsapp_message(
        org_id=user.org_id,
        content=content,
        conversation_id=conversation_id,
        customer_phone_no=phone,
        type="interactive",
        payload=body["interactive"],
        recipient_id=connection["wa_id"],
        role=MessageRole.AGENT,
        sender_name=user.full_name or "Agent",
        message_id=message_id,
        status="sent",
    )
    return result


@router.post("/conversations/{conversation_id}/calls")
@limiter.limit("10/minute")
async def start_whatsapp_call(request: Request, conversation_id: str, body: dict, user: CurrentUser):
    await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)
    if body.get("action") != "connect":
        raise HTTPException(status_code=422, detail="Only the connect action can start a call")
    session = _validated_call_session(body, "offer")
    connection, conversation, phone = _calling_context(user.org_id, conversation_id=conversation_id)
    result = await _meta_call_request(connection, "POST", "calls", json_body={
        "messaging_product": "whatsapp",
        "to": phone,
        "action": "connect",
        "session": session,
    })
    call_id = ((result.get("calls") or [{}])[0]).get("id")
    if not call_id:
        raise HTTPException(status_code=502, detail="Meta did not return a WhatsApp call ID")
    now = datetime.now(timezone.utc)
    device_id = body.get("device_id") if isinstance(body.get("device_id"), str) else None
    db.whatsapp_calls.update_one(
        {"org_id": user.org_id, "call_id": call_id},
        {
            "$setOnInsert": {
                "call_id": call_id,
                "org_id": user.org_id,
                "state": "ringing",
                "created_at": now,
                "started_at": now,
            },
            "$set": {
                "wa_id": connection["wa_id"],
                "business_phone_id": connection.get("phone_id"),
                "business_phone": connection.get("phone"),
                "conversation_id": conversation_id,
                "phone": phone,
                "contact_name": conversation.get("customer_name") or phone,
                "direction": "BUSINESS_INITIATED",
                "agent_id": user.user_id,
                "agent_name": user.full_name or "Agent",
                "agent_device_id": device_id,
                "handled_by": "human",
                "updated_at": now,
            },
        },
        upsert=True,
    )
    call = db.whatsapp_calls.find_one({"org_id": user.org_id, "call_id": call_id}, CALL_HISTORY_PROJECTION) or {}
    await broadcast_main_ws(connection["wa_id"], "whatsapp", "whatsapp_call", {
        "id": call_id,
        "call_id": call_id,
        "event": "started",
        "status": call.get("state") or "ringing",
        "direction": "BUSINESS_INITIATED",
        "conversation_id": conversation_id,
        "contact_name": conversation.get("customer_name") or phone,
        "phone": phone,
        "agent_id": user.user_id,
        "agent_name": user.full_name or "Agent",
        "agent_device_id": device_id,
        "handled_by": "human",
        "created_at": call.get("created_at"),
        "started_at": call.get("started_at"),
        "connected_at": call.get("connected_at"),
    })
    return {"call_id": call_id}


@router.get("/calls")
@limiter.limit("60/minute")
async def list_whatsapp_calls(
    request: Request,
    user: CurrentUser,
    conversation_id: str | None = None,
    before: str | None = None,
    limit: int = Query(30, ge=1, le=100),
):
    query: dict = {"org_id": user.org_id}
    if conversation_id:
        await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)
        query["conversation_id"] = conversation_id
    else:
        access = await conversation_access_condition(user.org_id, user.user_id, user.role)
        if access:
            allowed = await get_async_mongo_db()[f"conversations_{user.org_id}"].distinct("conversation_id", access)
            query["conversation_id"] = {"$in": allowed}
    if before:
        try:
            cursor_time, cursor_call_id = decode_call_cursor(before)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid call-history cursor") from exc
        query["$or"] = [
            {"updated_at": {"$lt": cursor_time}},
            {"updated_at": cursor_time, "call_id": {"$lt": cursor_call_id}},
        ]

    rows = await get_async_mongo_db().whatsapp_calls.find(
        query, CALL_HISTORY_PROJECTION
    ).sort([("updated_at", -1), ("call_id", -1)]).limit(limit + 1).to_list(length=limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_before = encode_call_cursor(rows[-1]["updated_at"], rows[-1]["call_id"]) if has_more and rows else None
    return {"items": serialize_mongo(rows), "next_before": next_before}


@router.get("/calls/{call_id}")
@limiter.limit("60/minute")
async def get_whatsapp_call(request: Request, call_id: str, user: CurrentUser):
    call = await get_async_mongo_db().whatsapp_calls.find_one(
        {"org_id": user.org_id, "call_id": call_id}, CALL_HISTORY_PROJECTION
    )
    if not call:
        raise HTTPException(status_code=404, detail="This WhatsApp call is no longer available")
    if call.get("conversation_id") and call.get("agent_id") != user.user_id:
        await require_conversation_access(
            user.org_id, call["conversation_id"], user.user_id, user.role
        )
    return serialize_mongo(call)


@router.post("/calls")
@limiter.limit("30/minute")
async def update_whatsapp_call(request: Request, body: dict, user: CurrentUser):
    action = body.get("action")
    call_id = body.get("call_id")
    if action not in {"pre_accept", "accept", "reject", "terminate"} or not isinstance(call_id, str) or not call_id.startswith("wacid."):
        raise HTTPException(status_code=422, detail="A valid WhatsApp call action and call ID are required")

    connection, _, _ = _calling_context(user.org_id, call_id=call_id)
    calls = db.whatsapp_calls
    call = calls.find_one({"call_id": call_id, "org_id": user.org_id})
    if canonical_call_state((call or {}).get("state")) in {"completed", "declined", "missed", "failed"}:
        raise HTTPException(status_code=409, detail="This WhatsApp call has already ended")
    owner = call.get("agent_id") if call else None
    if owner and owner != user.user_id:
        raise HTTPException(status_code=409, detail="Another teammate is already handling this call")

    claimed_now = not owner
    claim_token = uuid.uuid4().hex if claimed_now else None
    device_id = body.get("device_id") if isinstance(body.get("device_id"), str) else None
    if claimed_now:
        claim_fields = {
            "agent_id": user.user_id,
            "agent_name": user.full_name or "Agent",
            "handled_by": "human",
            "claim_operation_id": claim_token,
            "state": "connecting",
            "updated_at": datetime.now(timezone.utc),
        }
        if device_id:
            claim_fields["agent_device_id"] = device_id
        claimed = calls.find_one_and_update(
            {
                "call_id": call_id,
                "org_id": user.org_id,
                "state": {"$in": ["ringing", "connect", "started"]},
                "$or": [{"agent_id": None}, {"agent_id": user.user_id}, {"agent_id": {"$exists": False}}],
            },
            {"$set": claim_fields},
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            raise HTTPException(status_code=409, detail="Another teammate is already handling this call")

    payload = {"messaging_product": "whatsapp", "call_id": call_id, "action": action}
    if action in {"pre_accept", "accept"}:
        payload["session"] = _validated_call_session(body, "answer")
    try:
        result = await _meta_call_request(connection, "POST", "calls", json_body=payload)
    except Exception:
        if claimed_now:
            calls.update_one(
                {
                    "call_id": call_id,
                    "org_id": user.org_id,
                    "agent_id": user.user_id,
                    "claim_operation_id": claim_token,
                    "state": "connecting",
                },
                {
                    "$unset": {"agent_id": "", "agent_name": "", "agent_device_id": "", "handled_by": "", "claim_operation_id": ""},
                    "$set": {"state": "ringing", "updated_at": datetime.now(timezone.utc)},
                },
            )
        raise
    now = datetime.now(timezone.utc)
    current = calls.find_one({"call_id": call_id, "org_id": user.org_id}) or call or {}
    was_connected = bool(current.get("connected_at")) or canonical_call_state(current.get("state")) == "connected"
    state = {
        "pre_accept": "connecting",
        "accept": "connected",
        "reject": "declined",
        "terminate": "completed" if was_connected else ("missed" if current.get("direction") == "USER_INITIATED" else "failed"),
    }[action]
    fields = {
        "agent_id": user.user_id,
        "agent_name": user.full_name or "Agent",
        "handled_by": "human",
        "state": state,
        "updated_at": now,
    }
    if device_id:
        fields["agent_device_id"] = device_id
    if action == "accept":
        fields["connected_at"] = current.get("connected_at") or now
    if action in {"reject", "terminate"}:
        fields["ended_at"] = now
        connected_at = meta_datetime(current.get("connected_at"))
        if was_connected and connected_at:
            fields["duration_seconds"] = max(0, round((now - connected_at).total_seconds()))
    transition_filter = {
        "call_id": call_id,
        "org_id": user.org_id,
        "agent_id": user.user_id,
        "state": {"$nin": list(STORED_TERMINAL_CALL_STATES)},
    }
    if action == "pre_accept":
        transition_filter["state"] = "connecting"
        if claim_token:
            transition_filter["claim_operation_id"] = claim_token
    transition = calls.update_one(
        transition_filter,
        {"$set": fields, "$unset": {"claim_operation_id": ""}},
    )
    updated = calls.find_one({"call_id": call_id, "org_id": user.org_id}, CALL_HISTORY_PROJECTION) or fields
    actual_state = canonical_call_state(updated.get("state"))
    event_name = "terminate" if actual_state in {"completed", "declined", "missed", "failed"} else "accepted" if actual_state == "connected" else "claimed"
    await broadcast_main_ws(connection["wa_id"], "whatsapp", "whatsapp_call", {
        "id": call_id,
        "call_id": call_id,
        "event": event_name,
        "status": actual_state,
        "agent_id": updated.get("agent_id"),
        "agent_name": updated.get("agent_name"),
        "agent_device_id": updated.get("agent_device_id"),
        "handled_by": updated.get("handled_by"),
        "direction": updated.get("direction"),
        "conversation_id": updated.get("conversation_id"),
        "contact_name": updated.get("contact_name"),
        "phone": updated.get("phone"),
        "started_at": updated.get("started_at"),
        "connected_at": updated.get("connected_at"),
        "ended_at": updated.get("ended_at"),
        "duration_seconds": updated.get("duration_seconds"),
    })
    if action in {"pre_accept", "accept"} and not transition.matched_count and actual_state in {"completed", "declined", "missed", "failed"}:
        raise HTTPException(status_code=409, detail="This WhatsApp call has already ended")
    return result


@router.post("/auth")
@limiter.limit("10/minute")
async def whatsapp_auth_exchange(request: Request, req: WhatsappAuthRequest, user: CurrentUser):
    reservation_id = None
    meta_client = None
    try:
        await admin_required(user)
        meta_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        # Extract authorization code from request
        code = req.code
        # redirect_uri = request.redirect_uri
        org_id = user.org_id
        whatsapp_business_id = str(req.waba_id or "").strip()
        requested_phone_id = str(req.phone_number_id or "").strip()
        
        if not code:
            raise HTTPException(status_code=400, detail="Authorization code is required")
        if not whatsapp_business_id:
            raise HTTPException(status_code=422, detail="waba_id is required")
        if not requested_phone_id:
            raise HTTPException(status_code=422, detail="phone_number_id is required")

        active_connection_count = db.whatsapp_connections.count_documents({
            "org_id": org_id, "is_active": {"$ne": False},
        })
        if not _whatsapp_schema_ready(org_id):
            if active_connection_count:
                raise HTTPException(status_code=503, detail="WhatsApp number upgrade is still being finalized. Try again shortly.")
            await asyncio.to_thread(_initialize_whatsapp_schema, org_id)

        await _fence_whatsapp_reconnect_cleanup(org_id, requested_phone_id, whatsapp_business_id)

        # Exchange code for access token with WhatsApp/Facebook
        if not APP_ID or not APP_SECRET:
            logger.error("WhatsApp OAuth is not configured: APP_ID or APP_SECRET is missing")
            raise HTTPException(status_code=503, detail="WhatsApp connection is not configured. Contact support.")
        graph_version = WA_VERSION or VERSION or "v24.0"
        token_url = f"https://graph.facebook.com/{graph_version}/oauth/access_token"
        token_data = {
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "code": code,
        }

        token_response = await meta_client.get(token_url, params=token_data)
        
        if token_response.status_code != 200:
            try:
                meta_error = token_response.json().get("error", {})
            except (ValueError, AttributeError):
                meta_error = {}
            meta_code = meta_error.get("code") if isinstance(meta_error, dict) else None
            meta_subcode = meta_error.get("error_subcode") if isinstance(meta_error, dict) else None
            meta_trace = meta_error.get("fbtrace_id") if isinstance(meta_error, dict) else None
            logger.error(
                "WhatsApp OAuth exchange rejected: status={} code={} subcode={} trace_id={}",
                token_response.status_code, meta_code, meta_subcode, meta_trace,
            )
            reference = f" (Meta code {meta_code}{f'/{meta_subcode}' if meta_subcode else ''})" if meta_code else ""
            raise HTTPException(
                status_code=400,
                detail=f"Meta rejected the WhatsApp authorization. Start Add another number again.{reference}",
            )
        
        token_info = token_response.json()
        access_token = token_info.get("access_token")
        
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to get valid access token")
        
        # Get user phone number details
        params = {
            "fields": "id,cc,country_dial_code,display_phone_number,verified_name,status,quality_rating,search_visibility,platform_type,code_verification_status",
            "access_token": access_token # User's access token
        }
        phone_url = f"https://graph.facebook.com/{graph_version}/{whatsapp_business_id}/phone_numbers"
        phone_response = await meta_client.get(phone_url, params=params)
        if phone_response.status_code != 200:
            raise HTTPException(
                status_code=400, 
                detail="Failed to get phone number information"
            )
        
        phone_rows = phone_response.json().get("data")
        try:
            phones_data = select_exact_phone_number(phone_rows, requested_phone_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        phone = phones_data.get("display_phone_number")
        phone_verified = phones_data.get("code_verification_status")
        phone_id = str(phones_data.get("id") or "").strip()

        if db.whatsapp_connections.find_one({
            "$or": [{"phone_id": phone_id}, {"phone_number_id": phone_id}],
            "org_id": {"$ne": org_id},
            "is_active": {"$ne": False},
        }, {"_id": 1}):
            raise HTTPException(status_code=409, detail="This WhatsApp number is already connected to another organization")
        if db.whatsapp_connections.find_one({
            "$or": [{"wa_id": whatsapp_business_id}, {"waba_id": whatsapp_business_id}],
            "org_id": {"$ne": org_id},
            "is_active": {"$ne": False},
        }, {"_id": 1}):
            raise HTTPException(status_code=409, detail="This WhatsApp Business Account is already owned by another organization")
        _claim_whatsapp_waba_owner(org_id, whatsapp_business_id)

        active_connections = list(db.whatsapp_connections.find({
            "org_id": org_id,
            "is_active": {"$ne": False},
        }))
        try:
            validate_active_connection_slots(active_connections, limit=whatsapp_connection_limit(org_id))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"WhatsApp connection data needs repair: {exc}") from exc
        historical_connection = db.whatsapp_connections.find_one({
            "org_id": org_id,
            "$or": [{"phone_id": phone_id}, {"phone_number_id": phone_id}],
            "is_active": False,
        })
        limit = whatsapp_connection_limit(org_id)
        try:
            slot, existing_connection = choose_connection_slot(
                active_connections,
                phone_number_id=phone_id,
                waba_id=whatsapp_business_id,
                limit=limit,
            )
            existing_connection = existing_connection or historical_connection
            requested_label = req.label if req.label is not None else (existing_connection or {}).get("label")
            if not requested_label:
                verified_name = " ".join(str(phones_data.get("verified_name") or "").split())[:24]
                used_label_keys = {
                    connection_label_key(row.get("label"))
                    for row in active_connections
                    if row is not existing_connection and row.get("label")
                }
                requested_label = verified_name if connection_label_key(verified_name) not in used_label_keys else None
            label = normalize_connection_label(requested_label, slot)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not active_connections:
            reservation_id = reserve_capacity(org_id, "CHANNELS", "whatsapp_accounts", str(whatsapp_business_id))
        

        # subscribe user to our webhook
        webhook_params = {
            "access_token": access_token
        }
        webhook_url = f"https://graph.facebook.com/{graph_version}/{whatsapp_business_id}/subscribed_apps"
        webhook_response = await meta_client.post(webhook_url, json=webhook_params)

        if webhook_response.status_code != 200:
            raise HTTPException(
                status_code=400, 
                detail="Failed to subscribe WhatsApp account to webhook"
            )
        
        # TODO: test out if register step is needed or not 
        # Turns out it is needed for numbers that are not yet on WhatsApp but not needed for numbers that are already on WhatsApp
        # Add a check here for the same and only call register if the number is not on WhatsApp
        # the below check is useless
        
        # Check if phone number is verified
        if phone_verified != "VERIFIED":
            logger.warning(f"Phone number {phone} is currently {phone_verified}. Attempting registration to fix this...")
        
        # Register phone on WhatsApp Cloud API
        phone_register_params = {
            "messaging_product": "whatsapp",
            "pin": "000000",
            "access_token": access_token
        }
        phone_register_url = f"https://graph.facebook.com/{graph_version}/{phone_id}/register"

        try:
            phone_register_response = await meta_client.post(phone_register_url, json=phone_register_params, timeout=5.0)
            if phone_register_response.status_code != 200:
                logger.warning("WhatsApp phone registration returned status {}", phone_register_response.status_code)

        except Exception as e:
            logger.warning("WhatsApp phone registration request failed ({})", type(e).__name__)
        
        # Generate a unique user_id for this WhatsApp connection
        user_id = (existing_connection or {}).get("user_id") or str(uuid.uuid4())

        # Keep the legacy indexes, but let existing duplicate data be repaired by
        # the migration instead of failing a one-time OAuth exchange.
        for collection, index_field in (
            (db[f"messages_{org_id}"], "message_id"),
            (db[f"conversations_{org_id}"], "conversation_id"),
        ):
            try:
                collection.create_index(index_field, unique=True)
            except DuplicateKeyError:
                logger.warning(
                    "Skipped legacy WhatsApp {} unique index for org {} because duplicate records exist",
                    index_field,
                    org_id,
                )
            except OperationFailure as exc:
                if exc.code not in {85, 86}:
                    raise
                logger.warning(
                    "Kept existing legacy WhatsApp {} index for org {} because its options differ",
                    index_field,
                    org_id,
                )

        # Store connection in database
        now = datetime.now(timezone.utc)
        whatsapp_connection = {
            "user_id": user_id,
            "org_id": org_id,
            "wa_id": whatsapp_business_id,
            "waba_id": whatsapp_business_id,
            "phone": phone,
            "display_phone_number": phone,
            "phone_id": phone_id,
            "phone_number_id": phone_id,
            "name": phones_data.get("verified_name", "WhatsApp Business"),
            "label": label,
            "label_key": connection_label_key(label),
            "slot": slot,
            "access_token": access_token,
            "updated_at": now,
            "is_active": True,
            "business_info": {
                "status": "Active"
            }
        }

        await _fence_whatsapp_reconnect_cleanup(org_id, phone_id, whatsapp_business_id)
        
        # Store in your existing MongoDB database
        try:
            db.whatsapp_connections.update_one(
                {
                    "org_id": org_id,
                    "$or": [{"phone_id": phone_id}, {"phone_number_id": phone_id}],
                },
                {
                    "$set": whatsapp_connection,
                    "$setOnInsert": {"connected_at": now},
                    "$unset": {
                        "disconnected_at": "",
                        "last_slot": "",
                        "provider_revoke_status": "",
                        "provider_revoke_error": "",
                        "provider_revoke_attempts": "",
                        "provider_revoke_updated_at": "",
                        "superseded_by": "",
                    },
                },
                upsert=True,
            )
        except DuplicateKeyError as exc:
            raise HTTPException(
                status_code=409,
                detail="This WhatsApp number, slot, or label was claimed by another connection. Refresh and try again.",
            ) from exc
        db.whatsapp_connections.update_many(
            {
                "org_id": org_id,
                "$or": [{"wa_id": whatsapp_business_id}, {"waba_id": whatsapp_business_id}],
                "is_active": {"$ne": False},
            },
            {"$set": {"access_token": access_token, "updated_at": now}},
        )
        saved_connection = db.whatsapp_connections.find_one({
            "org_id": org_id,
            "$or": [{"phone_id": phone_id}, {"phone_number_id": phone_id}],
        }) or whatsapp_connection
        if saved_connection.get("slot") == 1:
            db.organizations.update_one(
                {"org_id": org_id},
                {"$set": {"wa_id": str(whatsapp_business_id)}},
            )
        
        return {
            "status": "success",
            "org_id": org_id,
            "user_id": user_id,
            "name": phones_data.get("verified_name", "WhatsApp Business"),
            "phone": phone,
            "connection": public_connection(saved_connection),
            "multi_number_enabled": limit > 1,
            "max_connections": limit,
            "max_numbers": limit,
            "business_info": {
                "account_type": "WhatsApp Business",
                "status": "Active"
            }
        }
        
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error("WhatsApp authentication failed for org {} ({})", user.org_id, type(e).__name__)
        raise HTTPException(status_code=502, detail="WhatsApp authentication failed") from e
    finally:
        if meta_client:
            await meta_client.aclose()
        release_capacity(reservation_id)
    
@router.post("/disconnect")
@limiter.limit("10/minute")
async def whatsapp_disconnect(
    request: Request,
    req: WhatsappDisconnectRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser,
):
    """Disconnect a WhatsApp Account from the system"""
    try:
        await admin_required(user)
        org_id = user.org_id
        connection = _find_owned_whatsapp_connection(
            org_id,
            connection_id=req.connection_id or req.user_id,
            phone_number_id=req.phone_id or req.phone_number_id,
            include_inactive=True,
        )
        if not connection:
            raise HTTPException(status_code=404, detail="WhatsApp connection not found")
        if not _whatsapp_schema_ready(org_id) and db.whatsapp_connections.count_documents({
            "org_id": org_id, "is_active": {"$ne": False},
        }) > 1:
            raise HTTPException(status_code=503, detail="WhatsApp number upgrade is still being finalized. Try again shortly.")
        connection_id = connection.get("phone_id") or connection.get("phone_number_id") or connection.get("wa_id")
        removal_result = await whatsapp_service.disconnect(org_id, connection_id)
        if removal_result.get("provider_cleanup") in {"pending", "failed"}:
            background_tasks.add_task(finalize_whatsapp_revocation, org_id, connection_id)
        return {
            "status": "disconnected",
            "phone_id": connection_id,
            **removal_result,
        }
       
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error disconnecting WhatsApp for org {} ({})", user.org_id, type(e).__name__)
        raise HTTPException(status_code=500, detail="Failed to disconnect WhatsApp") from e


@router.get("/connections")
@limiter.limit("30/minute")
async def list_whatsapp_connections(request: Request, user: CurrentUser):
    limit = whatsapp_connection_limit(user.org_id)
    connections = list(db.whatsapp_connections.find({
        "org_id": user.org_id,
        "is_active": {"$ne": False},
    }, {"_id": 0, "access_token": 0}).sort([("slot", 1), ("connected_at", 1)]))
    historical = list(db.whatsapp_connections.find({
        "org_id": user.org_id,
        "is_active": False,
    }, {"_id": 0, "access_token": 0}).sort("disconnected_at", -1))
    schema_ready = _whatsapp_schema_ready(user.org_id) or not connections
    try:
        connections = validate_active_connection_slots(connections, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"WhatsApp connection data needs repair: {exc}") from exc
    return {
        "connections": public_connections(connections),
        "historical_connections": public_historical_connections(historical),
        "multi_number_enabled": limit > 1,
        "max_connections": limit,
        "max_numbers": limit,
        "schema_ready": schema_ready,
        "can_add": schema_ready and len(connections) < limit,
    }


@router.patch("/connections/{connection_id}")
@router.patch("/connections/{connection_id}/label")
@limiter.limit("20/minute")
async def update_whatsapp_connection_label(
    request: Request,
    connection_id: str,
    body: WhatsappConnectionLabelRequest,
    user: CurrentUser,
):
    await admin_required(user)
    if not _whatsapp_schema_ready(user.org_id):
        raise HTTPException(status_code=503, detail="WhatsApp number upgrade is still being finalized. Try again shortly.")
    try:
        label = normalize_connection_label(body.label, 1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    value = connection_id.strip()
    if not value or len(value) > 128:
        raise HTTPException(status_code=422, detail="Invalid WhatsApp connection id")
    query = {
        "org_id": user.org_id,
        "is_active": {"$ne": False},
        "$or": [{"user_id": value}, {"phone_id": value}],
    }
    owned = db.whatsapp_connections.find_one(query, {"phone_id": 1})
    if not owned:
        raise HTTPException(status_code=404, detail="WhatsApp connection not found")
    label_key = connection_label_key(label)
    if db.whatsapp_connections.find_one({
        "org_id": user.org_id,
        "label_key": label_key,
        "phone_id": {"$ne": owned.get("phone_id")},
        "is_active": {"$ne": False},
    }):
        raise HTTPException(status_code=409, detail="WhatsApp number labels must be unique")
    try:
        connection = db.whatsapp_connections.find_one_and_update(
            query,
            {"$set": {"label": label, "label_key": label_key, "updated_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="WhatsApp number labels must be unique") from exc
    if not connection:
        raise HTTPException(status_code=404, detail="WhatsApp connection not found")
    return {"connection": public_connection(connection)}


@router.post("/connections/{phone_id}/primary")
@limiter.limit("20/minute")
async def make_whatsapp_connection_primary(request: Request, phone_id: str, user: CurrentUser):
    await admin_required(user)
    if not _whatsapp_schema_ready(user.org_id):
        raise HTTPException(status_code=503, detail="WhatsApp number upgrade is still being finalized. Try again shortly.")
    target = db.whatsapp_connections.find_one({
        "org_id": user.org_id,
        "$or": [{"phone_id": phone_id.strip()}, {"phone_number_id": phone_id.strip()}],
        "is_active": {"$ne": False},
    })
    if not target:
        raise HTTPException(status_code=404, detail="WhatsApp connection not found")
    active = list(db.whatsapp_connections.find({
        "org_id": user.org_id,
        "is_active": {"$ne": False},
    }).sort([("slot", 1), ("connected_at", 1)]))
    try:
        active = validate_active_connection_slots(active, limit=whatsapp_connection_limit(user.org_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"WhatsApp connection data needs repair: {exc}") from exc
    current = next((row for row in active if row.get("slot") == 1), active[0] if active else None)
    if current and current.get("_id") == target.get("_id"):
        return {"connection": public_connection(target)}

    now = datetime.now(timezone.utc)
    reordered = [target, *(row for row in active if row.get("_id") != target.get("_id"))]
    with db.client.start_session() as session:
        with session.start_transaction():
            if current:
                current_phone_id = str(current.get("phone_id") or current.get("phone_number_id") or "")
                db[f"conversations_{user.org_id}"].update_many(
                    {
                        "platform": "whatsapp",
                        "$or": [
                            {"business_phone_id": {"$exists": False}},
                            {"business_phone_id": None},
                        ],
                    },
                    {"$set": {"business_phone_id": current_phone_id, "business_phone": current.get("phone")}},
                    session=session,
                )
            for index, connection in enumerate(reordered, start=1):
                db.whatsapp_connections.update_one(
                    {"_id": connection["_id"]},
                    {"$set": {"slot": -index, "updated_at": now}},
                    session=session,
                )
            for slot, connection in enumerate(reordered, start=1):
                db.whatsapp_connections.update_one(
                    {"_id": connection["_id"]},
                    {"$set": {"slot": slot, "updated_at": now}},
                    session=session,
                )
            db.organizations.update_one(
                {"org_id": user.org_id},
                {"$set": {"wa_id": str(target.get("wa_id") or target.get("waba_id") or "")}},
                session=session,
            )
    target = db.whatsapp_connections.find_one({"_id": target["_id"]})
    return {"connection": public_connection(target)}

async def _get_whatsapp_details(org_id: str, selector: str):
    connection = _find_owned_whatsapp_connection(org_id, connection_id=selector)
    if not connection or not connection.get("access_token"):
        raise HTTPException(status_code=404, detail="WhatsApp connection not found or inactive")

    whatsapp_business_id = str(connection.get("wa_id") or connection.get("waba_id") or "")
    phone_id = str(connection.get("phone_id") or connection.get("phone_number_id") or "")
    if not whatsapp_business_id or not phone_id:
        raise HTTPException(status_code=409, detail="This WhatsApp connection is incomplete. Reconnect the number.")

    graph_version = WA_VERSION or VERSION or "v24.0"
    graph_url = f"https://graph.facebook.com/{graph_version}"
    headers = {"Authorization": f"Bearer {connection['access_token']}"}
    end_date = int(datetime.now(timezone.utc).timestamp())
    start_date = end_date - (30 * 24 * 60 * 60)
    warnings = []

    analytics_field = f"analytics.start({start_date}).end({end_date}).granularity(DAY)"
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        account_data, analytics_data, phone_data, templates_data = await asyncio.gather(
            _fetch_meta_details_resource(client, f"{graph_url}/{whatsapp_business_id}", headers, {"fields": "id,name,currency,timezone_id"}, "account details", org_id, warnings, required=True),
            _fetch_meta_details_resource(client, f"{graph_url}/{whatsapp_business_id}", headers, {"fields": analytics_field}, "analytics", org_id, warnings),
            _fetch_meta_details_resource(client, f"{graph_url}/{phone_id}", headers, {"fields": "display_phone_number,verified_name,code_verification_status,quality_rating,status,platform_type"}, "phone details", org_id, warnings),
            _fetch_meta_details_resource(client, f"{graph_url}/{whatsapp_business_id}/message_templates", headers, {"limit": 10}, "templates", org_id, warnings),
        )

    business_info = connection.get("business_info") or {}
    analytics = analytics_data.get("analytics")
    if not isinstance(analytics, dict):
        analytics = {"phone_numbers": [phone_id], "granularity": "DAY", "data_points": []}
    account_info = {
        "id": whatsapp_business_id,
        "name": connection.get("name") or business_info.get("verified_name") or "WhatsApp Business",
        "currency": "",
        "timezone_id": "",
        **account_data,
        "analytics": analytics,
    }
    phone_details = {
        "display_phone_number": connection.get("phone") or connection.get("display_phone_number") or "",
        "verified_name": connection.get("name") or "WhatsApp Business",
        "code_verification_status": business_info.get("verification_status") or "UNKNOWN",
        "quality_rating": business_info.get("quality_rating") or "UNKNOWN",
        "status": business_info.get("status") or "UNKNOWN",
        "platform_type": business_info.get("account_type") or "CLOUD_API",
        **phone_data,
    }
    templates = templates_data.get("data")
    if not isinstance(templates, list):
        templates = []
    return {
        "status": "partial" if warnings else "success",
        "warnings": warnings,
        "account_info": account_info,
        "phone_details": phone_details,
        "templates_summary": {"count": len(templates), "recent_templates": templates},
        "analytics_period": {
            "start": datetime.fromtimestamp(start_date, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end_date, timezone.utc).isoformat(),
            "granularity": "DAY",
        },
        "connection_details": {
            "user_id": connection.get("user_id"),
            "wa_id": whatsapp_business_id,
            "phone_id": phone_id,
        },
    }


@router.get("/connections/{phone_id}/details")
@limiter.limit("20/minute")
async def get_whatsapp_connection_details(request: Request, phone_id: str, user: CurrentUser):
    return await _get_whatsapp_details(user.org_id, phone_id)


@router.get("/{whatsapp_id}/details")
@limiter.limit("20/minute")
async def get_whatsapp_details(request: Request, whatsapp_id: str, user: CurrentUser):
    return await _get_whatsapp_details(user.org_id, whatsapp_id)

@router.get("/{whatsapp_id}/analytics/conversations")
@limiter.limit("30/minute")
async def get_whatsapp_conversation_analytics(
    request: Request,
    whatsapp_id: str,
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
    granularity: Optional[str] = "DAILY",
    phone_number: Optional[str] = None, 
    _: None = Depends(require_auth)
):
    """
    Fetch conversation analytics for a WhatsApp Business Account
    
    Args:
        whatsapp_id: The WhatsApp user ID from your application
        start_date: Start timestamp for analytics period (Unix timestamp)
        end_date: End timestamp for analytics period (Unix timestamp)
        granularity: Data granularity (DAILY, MONTHLY, or HALF_HOUR)
        phone_number: Optional specific phone number to filter analytics
        
    Returns:
        Conversation analytics data for the WhatsApp Business Account
    """
    try:
        # Find the WhatsApp connection to get the access token and business account ID
        connection = db.whatsapp_connections.find_one(
            {"user_id": whatsapp_id},
            {"wa_id": 1, "access_token": 1, "phone_id": 1, "_id": 0}
        )
        
        if not connection or not connection.get("access_token"):
            raise HTTPException(status_code=404, detail="WhatsApp connection not found or inactive")
        
        whatsapp_business_id = connection["wa_id"]
        access_token = connection["access_token"]
        
        # Set default date range if not provided (last 30 days)
        if not end_date:
            end_date = int(datetime.now().timestamp())
        
        if not start_date:
            # Default to 30 days before end_date
            start_date = end_date - (30 * 24 * 60 * 60)
        
        # Validate granularity
        valid_granularities = ["DAILY", "MONTHLY", "HALF_HOUR"]
        if granularity not in valid_granularities:
            granularity = "DAILY"
            
        # Define dimensions for analysis
        dimensions = ["PHONE", "COUNTRY", "CONVERSATION_TYPE", "CONVERSATION_DIRECTION"]
        
        # Build analytics field query including dimensions
        dimensions_param = ",".join(dimensions)
        conversation_analytics_field = (
            f"conversation_analytics.start({start_date}).end({end_date})."
            f"granularity({granularity}).dimensions({dimensions_param})."
            f"metric_types(CONVERSATION,COST)"
        )
        
        # Add phone number filter if provided
        if phone_number:
            conversation_analytics_field += f".phone_numbers({phone_number})"
            
        # Fetch WhatsApp Business Account analytics
        url = f"https://graph.facebook.com/v22.0/{whatsapp_business_id}"
        params = {
            "access_token": access_token,
            "fields": conversation_analytics_field
        }
        
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            logger.error(f"WhatsApp API Error: {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch WhatsApp conversation analytics: {response.json().get('error', {}).get('message')}"
            )
        
        analytics_data = response.json()
        
        # Process analytics data - calculate totals and format results
        conversation_data = analytics_data.get('conversation_analytics', {}).get('data', [])
        
        # Calculate summary statistics
        total_conversations = 0
        total_cost = 0
        business_initiated = 0
        user_initiated = 0
        country_stats = {}
        
        for data_group in conversation_data:
            for data_point in data_group.get('data_points', []):
                conv_count = data_point.get('conversation', 0)
                cost = data_point.get('cost', 0)
                direction = data_point.get('conversation_direction')
                country = data_point.get('country')
                
                total_conversations += conv_count
                total_cost += cost
                
                if direction == 'BUSINESS_INITIATED':
                    business_initiated += conv_count
                elif direction == 'USER_INITIATED':
                    user_initiated += conv_count
                
                if country:
                    if country not in country_stats:
                        country_stats[country] = {
                            'conversations': 0,
                            'cost': 0
                        }
                    country_stats[country]['conversations'] += conv_count
                    country_stats[country]['cost'] += cost
        
        # Extract and format the analytics data
        result = {
            "status": "success",
            "wa_id": whatsapp_business_id,
            "analytics_period": {
                "start": datetime.fromtimestamp(start_date).isoformat(),
                "end": datetime.fromtimestamp(end_date).isoformat(),
                "granularity": granularity
            },
            "summary": {
                "total_conversations": total_conversations,
                "total_cost": total_cost,
                "business_initiated": business_initiated,
                "user_initiated": user_initiated
            },
            "country_distribution": country_stats,
            "raw_data": analytics_data.get("conversation_analytics", {})
        }
        
        return result
        
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching WhatsApp conversation analytics: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch WhatsApp conversation analytics: {str(e)}"
        )

@router.get("/conversations")
@limiter.limit("30/minute")
async def get_whatsapp_conversations(request: Request, user: CurrentUser):
    """
    Fetch all conversations for a specific WhatsApp business account ID
    Returns them in the standard conversation format for compatibility with the frontend
    """
    try:
        org_id = user.org_id
        access = await conversation_access_condition(org_id, user.user_id, user.role)

        # Get the WhatsApp-specific collection name
        conversations_collection_name = f"conversations_{org_id}"
        
        # Check if collection exists in database
        if conversations_collection_name not in db.list_collection_names():
            return []
        
        # Get the collection
        conversations_collection = db[conversations_collection_name]
        
        # Fetch conversations and sort by last_message_timestamp
        platform_query = {"platform": "whatsapp"}
        query = {"$and": [platform_query, access]} if access else platform_query
        raw_conversations = list(conversations_collection.find(
            query,
            {"_id": 0}  # Exclude _id field
        ).sort("last_message_timestamp", -1))
        
        # Format conversations to match the required structure
        formatted_conversations = []
        for conv in raw_conversations:
            # Format timestamps
            timestamp = conv.get("last_message_timestamp") or conv.get("timestamp")
            if timestamp and isinstance(timestamp, datetime):
                timestamp = timestamp.isoformat()
            
            # Format the conversation object to match expected structure
            formatted_conversation = {
                'id': conv.get('conversation_id', conv.get('id')),
                'platform': 'whatsapp',
                'customer_id': conv.get('customer_id'),
                'customer_name': f"{conv.get('customer_name', '')}",
                'last_message': conv.get('last_message', ''),
                'timestamp': timestamp,
                'unread_count': conv.get('unread_count', 0),
                'is_ai_enabled': conv.get('is_ai_enabled', True),
                'assigned_agent_id': conv.get('assigned_agent_id'),
                'priority': conv.get('priority', 'medium'),  # Default to medium
                'sentiment': conv.get('sentiment', 'neutral')  # Default to neutral
            }
            
            formatted_conversations.append(formatted_conversation)
        
        return formatted_conversations
    
    except Exception as e:
        logger.error(f"Error fetching WhatsApp conversations for org {org_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch WhatsApp conversations: {str(e)}"
        )

@router.get("/conversations/{conversation_id}")
@limiter.limit("30/minute")
async def get_whatsapp_conversation(request: Request, conversation_id: str, user: CurrentUser):
    """
    Fetch a specific conversation for an Whatsapp business account ID
    Returns the conversation in the standard format for compatibility with the frontend
    """
    try:
        org_id = user.org_id
        await require_conversation_access(org_id, conversation_id, user.user_id, user.role)

        conversations_collection_name = f"conversations_{org_id}"
        
        # Check if collection exists in database
        if conversations_collection_name not in db.list_collection_names():
            return {"message": "No conversation found"}
        
        # Get the collection
        conversations_collection = db[conversations_collection_name]
        
        # Find the specific conversation
        conversation = conversations_collection.find_one(
            {"conversation_id": conversation_id},
            {"_id": 0}  # Exclude _id field
        )
        
        if not conversation:
            return {"message": "No conversation found"}
        
        # Format timestamps for JSON serialization
        if "last_message_timestamp" in conversation and isinstance(conversation["last_message_timestamp"], datetime):
            conversation["last_message_timestamp"] = conversation["last_message_timestamp"].isoformat()
        if "timestamp" in conversation and isinstance(conversation["timestamp"], datetime):
            conversation["timestamp"] = conversation["timestamp"].isoformat()
        
        return conversation
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching Instagram conversation {conversation_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch Instagram conversation: {str(e)}"
        )

@router.get("/conversations/{conversation_id}/messages")
@limiter.limit("30/minute")
async def get_whatsapp_messages(request: Request, conversation_id: str, user: CurrentUser):
    """Fetch messages for a specific WhatsApp conversation"""
    try:
        org_id = user.org_id
        await require_conversation_access(org_id, conversation_id, user.user_id, user.role)

        # Get the WhatsApp-specific collection name
        messages_collection_name = f"messages_{org_id}"
        
        # Check if collection exists
        if messages_collection_name not in db.list_collection_names():
            return []
        
        # Get the collection
        messages_collection = db[messages_collection_name]
        
        # Fetch messages
        messages = list(messages_collection.find(
            {"conversation_id": conversation_id},
            {"_id": 0, "raw_data": 0}  # Exclude _id field and raw_data to reduce payload size
        ).sort("timestamp", 1))  # Sort by timestamp ascending
        
        # Format timestamps for JSON serialization
        for msg in messages:
            if "timestamp" in msg and isinstance(msg["timestamp"], datetime):
                msg["timestamp"] = msg["timestamp"].isoformat()
        
        return messages
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching WhatsApp messages for {conversation_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch WhatsApp messages: {str(e)}"
        )

# @router.get("/conversations/{conversation_id}/read")
# async def update_whatsapp_unread_count(conversation_id: str, user: CurrentUser):
#     """Reset unread count for a specific WhatsApp conversation"""

#     try:
#         org_id = user.org_id 

#         # Get the WhatsApp-specific collection
#         conversations_collection = db[f"conversations_{org_id}"]
        
#         # Update the conversation
#         result = conversations_collection.update_one(
#             {"conversation_id": conversation_id},
#             {"$set": {"unread_count": 0}}
#         )
        
#         if result.matched_count == 0:
#             return {"status": "error", "message": "No conversation found"}
        
#         return {"status": "success", "message": "Unread count reset to 0"}
    
#     except Exception as e:
#         logger.error(f"Error resetting WhatsApp unread count: {e}")
#         raise HTTPException(
#             status_code=500, 
#             detail=f"Failed to reset unread count: {str(e)}"
#         )

@router.get("/conversations/{conversation_id}/ai")
@limiter.limit("30/minute")
async def get_whatsapp_ai_status(request: Request, conversation_id: str, user: CurrentUser):
    """Get AI status for a specific WhatsApp conversation"""
    try:

        org_id = user.org_id
        await require_conversation_access(org_id, conversation_id, user.user_id, user.role)
        # Get the WhatsApp-specific collection
        conversations_collection = db[f"conversations_{org_id}"]
        
        # Find the conversation
        conversation = conversations_collection.find_one({"conversation_id": conversation_id})
        
        if conversation:
            is_ai_enabled = conversation.get("is_ai_enabled", True)  # Default to True if not set
            return is_ai_enabled
        
        return {"status": "Error", "message": "No conversation found"}
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking WhatsApp AI status: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to check AI status: {str(e)}"
        )

@router.post("/conversations/{conversation_id}/toggle_ai")
@limiter.limit("30/minute")
async def toggle_whatsapp_ai_status(request: Request, conversation_id: str, body: dict, user: CurrentUser):
    """Toggle AI status for a specific WhatsApp conversation"""
    if "enabled" not in body:
        raise HTTPException(status_code=422, detail="enabled is required.")
    await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)
    action = "resume" if body["enabled"] else "pause"
    return await set_conversation_ai_state(user.org_id, conversation_id, action, body.get("duration_minutes"))

    # Legacy implementation retained below temporarily for rollback context.
    try:
        org_id = user.org_id 
        org_document = db.organizations.find_one({"org_id": org_id})
        if not org_document:
            raise HTTPException(status_code=404, detail="Organization not found")

        wa_id = org_document.get("wa_id")
        logger.success(f"Fetched wa_id: {wa_id} for org_id: {org_id}")
        # Get the enabled status from request body
        ai_enabled = body.get("enabled")
        auto_ai_disabled = bool(body.get("auto_disabled", False))
        
        if ai_enabled is None:
            return {"status": "error", "message": "Incorrect request body"}
        
        if ai_enabled and auto_ai_disabled:
            return {"status": "error", "message": "Cannot auto-disable AI while keeping it enabled"}
        
        # Get the WhatsApp-specific collection
        conversations_collection = db[f"conversations_{org_id}"]
        messages_collection = db[f"messages_{org_id}"]

        update_fields = {
            "is_ai_enabled": ai_enabled,
            "auto_ai_disabled": auto_ai_disabled
        }

        is_org_ai_enabled = org_document.get("is_ai_enabled", False)

        if not ai_enabled and auto_ai_disabled and is_org_ai_enabled:
            disable_duration = timedelta(minutes=5) if org_id == LEE_DEALER_ORG_ID else timedelta(hours=1)
            update_fields["auto_ai_disabled_until"] = datetime.now(timezone.utc) + disable_duration
        else:
            # Clear the auto-disable timer. Also force auto_ai_disabled=False when org AI is
            # disabled — prevents a stale True flag from being picked up if org AI is later re-enabled.
            update_fields["auto_ai_disabled_until"] = None
            if not is_org_ai_enabled:
                update_fields["auto_ai_disabled"] = False

        # Update the conversation
        result = conversations_collection.update_one(
            {"conversation_id": conversation_id},
            {"$set": update_fields}
        )
        
        if result.matched_count == 0:
            return {"status": "error", "message": "Conversation not found"}
        
        # Get updated conversation for broadcasting
        updated_conversation = conversations_collection.find_one({"conversation_id": conversation_id})

        # Determine if we should add a system message
        should_add_system_msg = False
        system_content = ""
        sender_id = "system"
        sender_name = "System"

        time_Str = "5 min" if org_id == LEE_DEALER_ORG_ID else '1 hr'

        # Only add system messages and broadcast if the state ACTUALLY changed
        if result.modified_count > 0:
            if ai_enabled == False and auto_ai_disabled == True and is_org_ai_enabled:
                should_add_system_msg = True
                system_content = f"Ai is disabled for this conversation, will be enabled after {time_Str}"
                sender_id = "system_auto"
                sender_name = "System (Auto)"
           
            if should_add_system_msg:
                msg_id = str(uuid.uuid4())
                msg_doc = {
                    "id": msg_id,
                    "message_id": msg_id,
                    "temp_id": None,
                    "conversation_id": conversation_id,
                    "platform": "internal",
                    "type": "system_message",
                    "content": system_content,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "role": "agent",
                    "timestamp": datetime.now(timezone.utc),
                    "status": "sent"
                }

                messages_collection.insert_one(msg_doc)

                broadcast_msg = {
                    "id": msg_doc["id"],
                    "platform": "whatsapp",
                    "conversation_id": msg_doc["conversation_id"],
                    "content": msg_doc["content"],
                    "sender_id": msg_doc["sender_id"],
                    "role": msg_doc["role"],
                    "timestamp": msg_doc["timestamp"].isoformat(),
                    "status": msg_doc["status"],
                    "sender_name": msg_doc["sender_name"],
                    "type": msg_doc["type"],
                    "temp_id": None,
                    "payload": None,
                    "mode": None,
                    "context_type": None,
                    "context": None
                }

                # Broadcast to main websocket manager
                await broadcast_main_ws(
                    platform_id=wa_id,
                    platform_type="whatsapp",
                    event_type="new_message",
                    payload={"message": broadcast_msg}
                )

                # Broadcast to WhatsApp-specific legacy connections
                if wa_id in services.whatsapp_connections_map:
                    logger.info(f"Broadcasting system message for AI status change to WhatsApp connections of wa_id: {wa_id}")
                    await broadcast_whatsapp_message(wa_id, "new_message", {
                        "message": broadcast_msg
                    })
        
        if updated_conversation and result.modified_count > 0:
            # Format for broadcasting
            broadcast_conversation = dict(updated_conversation)
            if "last_message_timestamp" in broadcast_conversation:
                broadcast_conversation["last_message_timestamp"] = broadcast_conversation["last_message_timestamp"].isoformat()
            if "_id" in broadcast_conversation:
                broadcast_conversation["_id"] = str(broadcast_conversation["_id"])
            
            # Broadcast to main websocket manager
            
            await broadcast_main_ws(
                platform_id=wa_id,
                platform_type="whatsapp",
                event_type="conversation_updated",
                payload={"conversation": broadcast_conversation}
            )

            # Broadcast to WhatsApp-specific connections
            if wa_id in services.whatsapp_connections_map:
                await broadcast_whatsapp_message(wa_id, "conversation_updated", {
                    "conversation": broadcast_conversation
                })
        
        return {"status": "success", "message": f"AI status updated to {ai_enabled}"}
    
    except Exception as e:
        logger.error(f"Error updating WhatsApp AI status: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to update AI status: {str(e)}"
        )

@router.post("/conversations/send_message")
@limiter.limit("30/minute")
async def process_whatsapp_message(request: Request, message: dict, user: CurrentUser):
    """Send a message to a WhatsApp conversation"""
    # Get Instagram-specific collections
    org_id = user.org_id
    await require_conversation_access(org_id, message["conversation_id"], user.user_id, user.role)
    org_conversations_collection = db[f"conversations_{org_id}"]
    
    # Check if conversation exists
    conversation = org_conversations_collection.find_one({"conversation_id": message["conversation_id"]})
    
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    res = await send_whatsapp_message(message, org_id)
    return res
    
# Not used anywhere
@router.post("/{whatsapp_id}/media/send")
@limiter.limit("30/minute")
async def upload_and_send_whatsapp_media(
    request: Request, 
    whatsapp_id: str, 
    recipient_id: str = Form(...),
    caption: Optional[str] = Form(None), 
    file: UploadFile = File(...), 
    _: None = Depends(require_auth)
):
    """
    Upload media to WhatsApp servers and send it to a recipient
    
    Args:
        whatsapp_id: The WhatsApp user ID from your application
        recipient_id: The phone number of the recipient (with country code)
        caption: Optional caption for the media
        file: The file to upload
    
    Returns:
        Status and message ID details along with the uploaded media URL
    """
    try:
        # Check storage limit before uploading
        storage_ok = await check_cloudinary_storage_limit()
        if not storage_ok:
            raise HTTPException(
                status_code=507,
                detail="Storage limit reached. Please contact support."
            )
        
        # Find the WhatsApp connection to get the access token and business account ID
        connection = db.whatsapp_connections.find_one(
            {"user_id": whatsapp_id},
            {"wa_id": 1, "access_token": 1, "phone_id": 1, "_id": 0}
        )
        
        if not connection or not connection.get("access_token"):
            raise HTTPException(status_code=404, detail="WhatsApp connection not found or inactive")
        
        wa_id = connection["wa_id"]
        access_token = connection["access_token"]
        phone_id = connection.get("phone_id")
        
        if not phone_id:
            raise HTTPException(status_code=400, detail="Phone ID not found in connection details")
        
        # First upload the file to Cloudinary to get a public URL that frontend can use
        cloudinary_result = cloudinary.uploader.upload(
            file.file,
            folder="whatsapp_media",
            resource_type="auto"
        )
        
        # Get the secure URL for the frontend to display the image
        media_url = cloudinary_result.get("secure_url")
        
        # Rewind the file to use it again for WhatsApp upload
        await file.seek(0)
        
        # Step 1: Upload media to WhatsApp servers
        upload_url = f"https://graph.facebook.com/v22.0/{phone_id}/media"
        
        # Read file content
        content = await file.read()
        content_type = file.content_type
        
        # Determine media type from content type
        if content_type.startswith('image/'):
            media_type = 'image'
        elif content_type.startswith('video/'):
            media_type = 'video'
        elif content_type.startswith('audio/'):
            media_type = 'audio'
        elif content_type == 'application/pdf':
            media_type = 'document'
        else:
            media_type = 'document'
        
        # Prepare upload request
        upload_files = {
            'file': (file.filename, content, content_type),
            'messaging_product': (None, 'whatsapp'),
            'type': (None, media_type)
        }
        
        upload_headers = {
            "Authorization": f"Bearer {access_token}"
        }

        # Make the upload request
        async with httpx.AsyncClient() as client:
            upload_response = await client.post(
                upload_url,
                headers=upload_headers,
                files=upload_files
            )
            
            if upload_response.status_code != 200:
                logger.error(f"WhatsApp Media Upload Error: {upload_response.text}")
                raise HTTPException(
                    status_code=upload_response.status_code,
                    detail=f"Failed to upload media to WhatsApp: {upload_response.text}"
                )
            
            upload_result = upload_response.json()
            media_id = upload_result.get("id")
            
            if not media_id:
                raise HTTPException(
                    status_code=400,
                    detail="No media ID returned from WhatsApp API"
                )
        
        # Step 2: Send media message using the obtained media ID
        send_url = f"https://graph.facebook.com/v22.0/{phone_id}/messages"
        send_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        send_data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": media_type,
            media_type: {
                "id": media_id,
            }
        }
        
        # Add caption if provided for media types that support it
        if caption and media_type in ['image', 'video', 'document']:
            send_data[media_type]["caption"] = caption
    
        # Send the media message
        async with httpx.AsyncClient() as client:
            send_response = await client.post(
                send_url,
                headers=send_headers,
                json=send_data
            )
            
            if send_response.status_code != 200:
                logger.error(f"WhatsApp Media Send Error: {send_response.text}")
                raise HTTPException(
                    status_code=send_response.status_code,
                    detail=f"Failed to send WhatsApp media message: {send_response.text}"
                )
            
            send_result = send_response.json()
            message_id = send_result.get("messages", [{}])[0].get("id")
        
        # Create conversation ID for this recipient
        conversation_id = f"whatsapp_{recipient_id}"
        
        # Store message in WhatsApp-specific collection
        await store_whatsapp_message(
            content=caption or f"[{media_type.capitalize()}]",
            conversation_id=conversation_id,
            sender_id="agent",
            sender_name="Agent",
            recipient_id=wa_id,
            role=MessageRole.AGENT,
            message_id=message_id,
            message_type=media_type,
            raw_data={
                "media_id": media_id, 
                "filename": file.filename, 
                "mime_type": content_type,
                "media_url": media_url  # Store the Cloudinary URL in the message
            }
        )
        
        # Return success response with details including the media URL
        return {
            "status": "success",
            "message": "Media uploaded and sent successfully",
            "details": {
                "message_id": message_id,
                "media_id": media_id,
                "media_type": media_type,
                "recipient_id": recipient_id,
                "file_name": file.filename,
                "media_url": media_url  # Return the media URL to frontend
            }
        }
        
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error uploading and sending WhatsApp media: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload and send media: {str(e)}"
        )

@router.get("/profile")
@limiter.limit("30/minute")
async def get_whatsapp_profile(request: Request, user: CurrentUser):
    """
    Fetch detailed WhatsApp profile information
    
    Returns:
        WhatsApp profile information.
    """
    try:
        org_id = user.org_id 

        org_document = db.organizations.find_one({"org_id": org_id})

        wa_id = org_document.get("wa_id")
        logger.success(f"Fetched wa_id: {wa_id} for org_id: {org_id}")
    
        if not wa_id:
            raise HTTPException(status_code=400, detail="WhatsApp ID not configured for this organization")

        # Get the page access token for this WhatsApp account
        connection = db.whatsapp_connections.find_one(
            {"wa_id": wa_id, "org_id": org_id},
            {"_id": 0}
        )
        logger.success("Fetched WhatsApp connection for wa_id: {}", wa_id)

        if not connection or not connection.get("access_token"):
            raise HTTPException(status_code=404, detail="WhatsApp connection not found or inactive")
        
        connection = serialize_mongo(connection)

        return {
            "user_id": connection.get("user_id"),
            "business_info": connection.get("business_info", {}),
            "name" : connection.get("name"),
            "phone" : connection.get("phone"),
        }
    
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching WhatsApp profile: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch WhatsApp profile: {str(e)}")

@router.get("/templates")
@limiter.limit("60/minute")
async def fetch_templates(request: Request, user: CurrentUser):
    """
    fetch prebuilt templates:
    """
    try:
        org_id = user.org_id
        org_document = db.organizations.find_one({"org_id": org_id})

        wa_id = org_document.get("wa_id")
        logger.success(f"Fetched wa_id: {wa_id} for org_id: {org_id}")

        connection = db.whatsapp_connections.find_one(
            {"wa_id": wa_id, "org_id": org_id},
            {"_id": 0, "access_token": 1}
        )

        access_token = connection["access_token"] if connection else None

        if not connection or "access_token" not in connection:
            raise HTTPException(
                status_code=404,
                detail="WhatsApp connection not found or access token missing"
            )

        logger.success(f"Fetched WhatsApp connection for wa_id: {wa_id}")


        templates = await get_templates(org_id, wa_id, access_token)

        return {
            "status": "success",
            "message": "templates fetched successfully",
            "templates": templates
        }

    except Exception as e:
        logger.error(f"Error fetching prebuilt templates: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch prebuilt templates: {str(e)}")

@router.post("/templates")
@limiter.limit("30/minute")
async def whatsapp_create_template(request: Request, user: CurrentUser, template: Template):
    """
        Create templates
    """
    try:
        org_id = user.org_id
        org_document = db.organizations.find_one({"org_id": org_id})

        wa_id = org_document.get("wa_id")
        logger.success(f"Fetched wa_id: {wa_id} for org_id: {org_id}")

        connection = db.whatsapp_connections.find_one(
            {"wa_id": wa_id, "org_id": org_id},
            {"_id": 0, "access_token": 1}
        )

        access_token = connection["access_token"] if connection else None

        if not connection or "access_token" not in connection:
            raise HTTPException(
                status_code=404,
                detail="WhatsApp connection not found or access token missing"
            )

        logger.info(f"Creating WhatsApp template: name={template.name}, language={template.language}, category={template.category}, components_count={len(template.components)}")
        
        templates = await create_templates(org_id, wa_id, access_token, template.name, template.language, template.category, template.components)

        return {
            "status": "success",
            "message": "templates sent for review",
            "templates": templates
        }

    except HTTPException as e:
        # Re-raise HTTPException to preserve status code and detail from create_templates
        logger.error(f"HTTP Error creating templates: {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"Unexpected error creating templates: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create templates: {str(e)}")

@router.delete("/templates/{template_name}")
@limiter.limit("30/minute")
async def delete_whatsapp_template(request: Request, template_name: str, user: CurrentUser):
    """
    Delete a WhatsApp template by name.
    """
    try:
        org_id = user.org_id
        org_document = db.organizations.find_one({"org_id": org_id})

        if not org_document:
            raise HTTPException(status_code=404, detail="Organization not found")

        wa_id = org_document.get("wa_id")
        
        if not wa_id:
            raise HTTPException(status_code=400, detail="WhatsApp ID not configured for this organization")

        connection = db.whatsapp_connections.find_one(
            {"wa_id": wa_id, "org_id": org_id},
            {"_id": 0, "access_token": 1}
        )

        if not connection or "access_token" not in connection:
            raise HTTPException(
                status_code=404,
                detail="WhatsApp connection not found or access token missing"
            )

        access_token = connection["access_token"]
        
        # Split template names if multiple are provided (e.g., "template1,template2")
        template_names = [name.strip() for name in template_name.split(",")]
        
        url = f"https://graph.facebook.com/v24.0/{wa_id}/message_templates"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        results = []
        errors = []

        async with httpx.AsyncClient() as client:
            for name in template_names:
                params = {"name": name}
                try:
                    response = await client.delete(url, headers=headers, params=params)
                    if response.status_code == 200:
                        results.append(name)
                        logger.success(f"Successfully deleted WhatsApp template: {name}")
                    else:
                        try:
                            error_data = response.json()
                            error_msg = error_data.get("error", {}).get("message", "Unknown error")
                        except ValueError:
                            body_text = response.text or ""
                            error_msg = body_text if body_text.strip() else "Unknown error"
                        errors.append({"name": name, "error": error_msg})
                        logger.error(f"Failed to delete WhatsApp template {name}: {error_msg}")
                except Exception as e:
                    errors.append({"name": name, "error": str(e)})
                    logger.error(f"Exception deleting WhatsApp template {name}: {str(e)}")

        if errors and not results:
            # If everything failed
            raise HTTPException(
                status_code=400,
                detail={"message": "Failed to delete templates", "errors": errors}
            )

        return {
            "status": "success",
            "message": f"Templates deleted successfully",
            "deleted": results,
            "failed": errors
        }

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Unexpected error deleting WhatsApp template: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete template: {str(e)}")

@router.post("/bulk/send")
@limiter.limit("30/minute")
async def bulk_send_whatsapp_message(
    request: Request,
    req: BulkMessageRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser,
):
    """
    Enqueue a bulk WhatsApp template send as a background job.

    Returns immediately with a `job_id`. Poll `/sse/bulk-status/{job_id}` to track progress.
    - template_name: Name of an APPROVED template in your WhatsApp Business account.
    - language_code: BCP-47 code (e.g. 'en', 'en_US', 'hi').
    - recipients: List of objects with `phone_number` (E.164 digits only).
    - delay_seconds: Pause between batches of sends in seconds (minimum 8.0, default 8.0). Each batch
      can include up to `max_concurrent` recipients.
    """
    try:
        org_id = user.org_id
        from services.entitlement_service import consume_quota_async, feature_access, release_quota_async, require_feature

        require_feature(org_id, "BROADCASTING")
        unique_recipients = {}
        for recipient in req.recipients:
            phone = "".join(character for character in recipient.phone_number if character.isdigit())
            unique_recipients[phone] = recipient.model_copy(update={"phone_number": phone})
        recipients = list(unique_recipients.values())
        suppressed = await asyncio.to_thread(
            get_broadcast_suppressed_phone_keys,
            org_id,
            [recipient.phone_number for recipient in recipients],
        )
        recipients = [recipient for recipient in recipients if recipient.phone_number not in suppressed]
        excluded_opt_outs = len(unique_recipients) - len(recipients)
        logger.info(f"Bulk WA send request received | org_id={org_id} | recipients={len(recipients)}")

        if not recipients:
            raise HTTPException(status_code=409, detail="All selected recipients have opted out of broadcasts")
        batch_limit = feature_access(org_id, "BROADCASTING")["limits"].get("max_batch_recipients")
        if batch_limit is not None and len(recipients) > batch_limit:
            raise HTTPException(status_code=422, detail={"code": "broadcast_batch_limit", "feature": "BROADCASTING", "metric": "max_batch_recipients", "used": len(recipients), "limit": batch_limit, "message": f"This broadcast exceeds the {batch_limit}-recipient batch limit."})

        # Look up the org's WhatsApp connection
        org_document = db.organizations.find_one({"org_id": org_id})
        if not org_document:
            raise HTTPException(status_code=404, detail="Organization not found")

        wa_id = org_document.get("wa_id")
        if not wa_id:
            raise HTTPException(status_code=404, detail="No WhatsApp account connected to this organization")

        connection = resolve_outbound_whatsapp_connection(
            org_id,
            wa_id,
            req.phone_number_id,
        )

        if not connection or not connection.get("access_token"):
            raise HTTPException(
                status_code=409,
                detail="Select a WhatsApp number before starting this bulk send"
            )

        access_token = connection.get("access_token")
        phone_id = connection.get("phone_id")

        if not phone_id:
            raise HTTPException(status_code=400, detail="Phone ID not found for this WhatsApp connection")

        # Create the job record off the event loop so the client gets the job_id immediately
        job_id = uuid.uuid4().hex
        quota_event_key = f"broadcast:{job_id}"
        await consume_quota_async(org_id, "BROADCASTING", "monthly_recipients", len(recipients), quota_event_key)
        try:
            await asyncio.to_thread(
                create_bulk_job,
                org_id=org_id,
                job_id=job_id,
                template_name=req.template_name,
                language_code=req.language_code,
                total=len(recipients),
            )
        except Exception:
            await release_quota_async(org_id, "monthly_recipients", quota_event_key)
            raise

        # Enqueue the heavy work as a background task
        background_tasks.add_task(
            run_bulk_send_job,
            wa_id=wa_id,
            job_id=job_id,
            org_id=org_id,
            phone_id=phone_id,
            access_token=access_token,
            template_name=req.template_name,
            language_code=req.language_code,
            recipients=recipients,
            components=req.components,
            delay_seconds=req.delay_seconds,
            # max_concurrent=20, # Optional: can be passed if needed
        )

        logger.success(f"Bulk WA | job={job_id} enqueued | org={org_id} | recipients={len(recipients)}")

        return {
            "status": "accepted",
            "message": "Bulk send job enqueued.",
            "job_id": job_id,
            "total": len(recipients),
            "excluded_opt_outs": excluded_opt_outs,
        }
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Error enqueueing bulk WhatsApp send: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Bulk send failed: {str(e)}")
