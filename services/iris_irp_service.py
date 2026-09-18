"""Fail-closed IRIS IRP6 core API boundary and encrypted credential storage.

The public IRIS OpenAPI contract documents the HTTP shape, but not the
cryptographic algorithm used to create ``Data`` or unwrap the session key.
Consequently the default codec deliberately cannot issue requests.  A vetted
IRIS-provided codec must be injected before this adapter is enabled.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Awaitable, Callable, Literal, Protocol
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pymongo import ReturnDocument

from config.settings import (
    IRIS_IRP_API_BASE_URL,
    IRIS_IRP_CLIENT_ID,
    IRIS_IRP_CLIENT_SECRET,
    IRIS_IRP_ENV,
    IRIS_IRP_PUBLIC_KEY_PEM,
    IRP_CREDENTIAL_ENCRYPTION_KEY,
)
from database import get_async_mongo_db
from services.india_tax import GST_STATE_NAMES, is_valid_gstin


db = get_async_mongo_db()
credentials = db.iris_irp_credentials

# The current public Core API OpenAPI document publishes only this sandbox
# origin.  Production stays blocked until IRIS supplies and verifies its exact
# Core API origin; suffix matching would allow an attacker-controlled subdomain.
_APPROVED_CORE_HOSTS = {
    "sandbox": frozenset({"api.sandbox.core.irisirp.com"}),
    "production": frozenset(),
}
_IRN_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CORE_PATHS = frozenset({
    "/eivital/v1.04/auth",
    "/eicore/v1.03/Invoice",
    "/eicore/v1.03/Invoice/Cancel",
    "/eicore/v1.03/Invoice/irnbydocdetails",
})
_TOKEN_CACHE_TTL = timedelta(hours=5, minutes=45)  # official token lifetime: 6h
_MAX_ENCRYPTED_RESPONSE = 8 * 1024 * 1024
_MAX_SIGNED_ARTIFACT = 4 * 1024 * 1024


class IrisIrpCodecUnavailable(RuntimeError):
    """Raised when the official IRIS cryptographic implementation is absent."""


class IrisIrpOutcomeUnknown(RuntimeError):
    """A mutating request may have reached IRIS and must be reconciled."""


@dataclass(frozen=True)
class IrisIrpConfig:
    environment: Literal["sandbox", "production"]
    base_url: str
    client_id: str
    client_secret: str
    public_key_pem: str


@dataclass(frozen=True)
class AuthEnvelope:
    data: str
    context: Any


@dataclass(frozen=True)
class AuthSession:
    username: str
    auth_token: str
    session_key: str
    expires_at: datetime


class IrisIrpCodec(Protocol):
    """Crypto seam implemented only from the private IRIS onboarding spec."""

    def encrypt_auth(self, username: str, password: str, public_key_pem: str) -> AuthEnvelope: ...

    def complete_auth(
        self, response_data: dict, context: Any, *, now: datetime
    ) -> AuthSession: ...

    def encrypt_core(self, payload: dict, session_key: str) -> str: ...

    def decrypt_core(self, data: str, session_key: str) -> dict: ...


class IrisIrpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict | None = None,
        params: dict[str, str] | None = None,
    ) -> Any: ...


class UnconfiguredIrisIrpCodec:
    """Production default: never invent undocumented IRIS cryptography."""

    @staticmethod
    def _blocked() -> None:
        raise IrisIrpCodecUnavailable(
            "IRIS IRP crypto codec is not configured from the official partner specification"
        )

    def encrypt_auth(self, username: str, password: str, public_key_pem: str) -> AuthEnvelope:
        self._blocked()

    def complete_auth(self, response_data: dict, context: Any, *, now: datetime) -> AuthSession:
        self._blocked()

    def encrypt_core(self, payload: dict, session_key: str) -> str:
        self._blocked()

    def decrypt_core(self, data: str, session_key: str) -> dict:
        self._blocked()


class HttpxIrisIrpTransport:
    """TLS-verifying transport with redirects disabled and bounded timeouts."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            verify=True,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=3.0, read=12.0, write=12.0, pool=3.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.request(method, url, **kwargs)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _fernet() -> Fernet:
    if not IRP_CREDENTIAL_ENCRYPTION_KEY:
        raise HTTPException(status_code=503, detail="IRP credential encryption is not configured")
    try:
        return Fernet(IRP_CREDENTIAL_ENCRYPTION_KEY.encode())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="IRP credential encryption is misconfigured") from exc


def validate_base_url(base_url: str, environment: str) -> str:
    if environment not in _APPROVED_CORE_HOSTS:
        raise HTTPException(status_code=503, detail="IRIS IRP environment is invalid")
    if not base_url:
        raise HTTPException(status_code=503, detail="IRIS IRP API endpoint is not configured")
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="IRIS IRP API endpoint is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or host not in _APPROVED_CORE_HOSTS[environment]
    ):
        raise HTTPException(status_code=503, detail="IRIS IRP API endpoint is not an approved HTTPS host")
    return f"https://{host}"


def _validate_base_url() -> None:
    validate_base_url(IRIS_IRP_API_BASE_URL, IRIS_IRP_ENV)


def require_iris_irp_configuration() -> None:
    if IRIS_IRP_ENV not in {"sandbox", "production"}:
        raise HTTPException(status_code=503, detail="IRIS IRP environment is invalid")
    _validate_base_url()
    if not all((IRIS_IRP_CLIENT_ID, IRIS_IRP_CLIENT_SECRET, IRIS_IRP_PUBLIC_KEY_PEM)):
        raise HTTPException(status_code=503, detail="IRIS IRP client credentials are incomplete")
    _fernet()


async def ensure_iris_irp_indexes() -> None:
    await credentials.create_index([("org_id", 1), ("gstin", 1), ("environment", 1)], unique=True)


async def save_taxpayer_credentials(
    org_id: str,
    gstin: str,
    username: str,
    password: str,
    actor_id: str,
) -> dict:
    gstin = gstin.strip().upper()
    username = username.strip()
    if not is_valid_gstin(gstin):
        raise HTTPException(status_code=422, detail="Enter a valid GSTIN with checksum")
    if not 1 <= len(username) <= 15:
        raise HTTPException(status_code=422, detail="IRP username must be between 1 and 15 characters")
    if not 8 <= len(password) <= 16:
        raise HTTPException(status_code=422, detail="IRP password must be between 8 and 16 characters")
    require_iris_irp_configuration()
    cipher = _fernet()
    now = datetime.now(timezone.utc)
    document = await credentials.find_one_and_update(
        {"org_id": org_id, "gstin": gstin, "environment": IRIS_IRP_ENV},
        {
            "$set": {
                "username_encrypted": cipher.encrypt(username.encode()).decode(),
                "password_encrypted": cipher.encrypt(password.encode()).decode(),
                "updated_at": now,
                "updated_by": actor_id,
                "rotation_due_at": now + timedelta(days=60),
                "verified_at": None,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return credential_status_view(document)


def credential_status_view(document: dict | None) -> dict:
    if not document:
        return {"configured": False, "environment": IRIS_IRP_ENV}
    return {
        "configured": True,
        "verified": document.get("verified_at") is not None,
        "environment": document.get("environment"),
        "gstin": document.get("gstin"),
        "updated_at": document.get("updated_at"),
        "rotation_due_at": document.get("rotation_due_at"),
        "verified_at": document.get("verified_at"),
    }


async def taxpayer_credential_status(org_id: str, gstin: str = "") -> dict:
    query: dict = {"org_id": org_id, "environment": IRIS_IRP_ENV}
    if gstin:
        query["gstin"] = gstin.strip().upper()
    return credential_status_view(await credentials.find_one(query))


async def load_taxpayer_credentials(org_id: str, gstin: str) -> tuple[str, str]:
    document = await credentials.find_one({
        "org_id": org_id,
        "gstin": gstin.strip().upper(),
        "environment": IRIS_IRP_ENV,
    })
    if not document:
        raise HTTPException(status_code=409, detail="IRP credentials are not configured for the seller GSTIN")
    try:
        cipher = _fernet()
        return (
            cipher.decrypt(document["username_encrypted"].encode()).decode(),
            cipher.decrypt(document["password_encrypted"].encode()).decode(),
        )
    except (InvalidToken, KeyError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Stored IRP credentials cannot be decrypted") from exc


def _parse_date(value: str | date, field: str) -> date:
    try:
        if isinstance(value, date):
            return value
        text = str(value).strip()
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", text):
            return datetime.strptime(text, "%d/%m/%Y").date()
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be a valid date") from exc


def _irp_date(value: str | date, field: str = "document date") -> str:
    return _parse_date(value, field).strftime("%d/%m/%Y")


def _money(paise: int) -> int | float:
    value = (Decimal(int(paise)) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(value) if value == value.to_integral() else float(value)


def _decimal_number(value: Any, field: str) -> int | float:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be numeric") from exc
    if not number.is_finite() or number <= 0:
        raise HTTPException(status_code=422, detail=f"{field} must be greater than zero")
    return int(number) if number == number.to_integral() else float(number)


def _required_text(value: Any, field: str, *, maximum: int = 500) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise HTTPException(status_code=422, detail=f"{field} is missing or invalid")
    return text


def _party_payload(party: dict, *, buyer: bool, place_of_supply: str = "") -> dict:
    gstin = str(party.get("gstin") or "").strip().upper()
    if not is_valid_gstin(gstin):
        raise HTTPException(status_code=422, detail=f"{'Buyer' if buyer else 'Seller'} GSTIN is invalid")
    state = _required_text(party.get("state_code"), "party state code", maximum=2)
    pin = str(party.get("pincode") or "").strip()
    if gstin[:2] != state or not re.fullmatch(r"\d{6}", pin):
        raise HTTPException(status_code=422, detail="Party GSTIN, state code, or pincode is inconsistent")
    address = _required_text(party.get("address"), "party address")
    result = {
        "Gstin": gstin,
        "LglNm": _required_text(party.get("legal_name") or party.get("name"), "party legal name", maximum=120),
        "TrdNm": str(party.get("trade_name") or party.get("name") or "").strip()[:120] or None,
        "Addr1": address[:100],
        "Addr2": address[100:200] or None,
        "Loc": _required_text(party.get("location"), "party location", maximum=120),
        "Pin": int(pin),
        "Stcd": state,
        "Ph": str(party.get("phone") or "").strip() or None,
        "Em": str(party.get("email") or "").strip() or None,
    }
    if buyer:
        result["Pos"] = _required_text(place_of_supply, "place of supply", maximum=2)
    return result


def enforce_reporting_window(
    document_date: str | date,
    *,
    aato_10cr_reporting_limit_applies: bool,
    today: date | None = None,
) -> None:
    """Enforce the official 30-day INV/CRN/DBN reporting restriction."""

    issued = _parse_date(document_date, "document date")
    current = today or datetime.now(timezone.utc).astimezone().date()
    if issued > current:
        raise HTTPException(status_code=422, detail="IRP document date cannot be in the future")
    if aato_10cr_reporting_limit_applies and (current - issued).days > 30:
        raise HTTPException(
            status_code=409,
            detail="The IRP 30-day reporting window has expired for this taxpayer",
        )


def map_domestic_b2b_document(document: dict, *, document_type: str | None = None) -> dict:
    """Map a finalized native invoice/note snapshot to IRIS schema version 1.1."""

    if document.get("currency") != "INR" or document.get("supply_category") != "domestic":
        raise HTTPException(status_code=422, detail="IRIS mapping supports domestic INR documents only")
    kind = document_type or document.get("document_kind") or document.get("adjustment_type") or "invoice"
    type_code = {"invoice": "INV", "credit_note": "CRN", "debit_note": "DBN"}.get(str(kind))
    if not type_code:
        raise HTTPException(status_code=422, detail="Only tax invoices, credit notes, and debit notes are supported")
    number_field = "invoice_number" if type_code == "INV" else "note_number"
    date_field = "invoice_date" if type_code == "INV" else "note_date"
    number = _required_text(document.get(number_field), "document number", maximum=16)
    document_date = document.get(date_field) or document.get("invoice_date")
    seller = _party_payload(document.get("seller") or {}, buyer=False)
    buyer = _party_payload(
        document.get("customer") or document.get("buyer") or {},
        buyer=True,
        place_of_supply=str(document.get("place_of_supply_state_code") or ""),
    )
    if seller["Gstin"] == buyer["Gstin"]:
        raise HTTPException(status_code=422, detail="Seller and buyer GSTIN cannot be identical for B2B mapping")

    mapped_items: list[dict] = []
    for index, item in enumerate(document.get("items") or [], start=1):
        quantity = Decimal(str(_decimal_number(item.get("quantity"), "item quantity")))
        taxable_before_discount = int(item.get("taxable_before_discount_paise") or 0)
        taxable = int(item.get("taxable_value_paise") or 0)
        discount = int(item.get("discount_paise") or 0)
        cgst = int(item.get("cgst_paise") or 0)
        sgst = int(item.get("sgst_paise") or 0)
        igst = int(item.get("igst_paise") or 0)
        total = int(item.get("total_paise") or 0)
        if taxable_before_discount < 0 or taxable < 0 or discount < 0 or total != taxable + cgst + sgst + igst:
            raise HTTPException(status_code=422, detail="Invoice item tax arithmetic is inconsistent")
        unit_price = (Decimal(taxable_before_discount) / Decimal(100) / quantity).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        mapped_items.append({
            "SlNo": str(index),
            "PrdDesc": _required_text(item.get("description"), "item description"),
            "IsServc": "Y" if item.get("supply_type") == "services" else "N",
            "HsnCd": _required_text(item.get("hsn_sac"), "HSN/SAC", maximum=12),
            "Qty": int(quantity) if quantity == quantity.to_integral() else float(quantity),
            "Unit": _required_text(item.get("uqc"), "item unit", maximum=10),
            "UnitPrice": int(unit_price) if unit_price == unit_price.to_integral() else float(unit_price),
            "TotAmt": _money(taxable_before_discount),
            "Discount": _money(discount),
            "AssAmt": _money(taxable),
            "GstRt": int(item.get("gst_rate_bps") or 0) / 100,
            "IgstAmt": _money(igst),
            "CgstAmt": _money(cgst),
            "SgstAmt": _money(sgst),
            "TotItemVal": _money(total),
        })
    if not mapped_items:
        raise HTTPException(status_code=422, detail="IRP document needs at least one item")

    payload = {
        "Version": "1.1",
        "TranDtls": {
            "TaxSch": "GST",
            "SupTyp": "B2B",
            "RegRev": "Y" if document.get("reverse_charge") else "N",
            "EcmGstin": None,
            "IgstOnIntra": "N",
        },
        "DocDtls": {"Typ": type_code, "No": number, "Dt": _irp_date(document_date)},
        "SellerDtls": seller,
        "BuyerDtls": buyer,
        "ItemList": mapped_items,
        "ValDtls": {
            "AssVal": _money(int(document.get("taxable_value_paise") or 0)),
            "CgstVal": _money(int(document.get("cgst_paise") or 0)),
            "SgstVal": _money(int(document.get("sgst_paise") or 0)),
            "IgstVal": _money(int(document.get("igst_paise") or 0)),
            "Discount": _money(int(document.get("discount_paise") or 0)),
            "TotInvVal": _money(int(document.get("total_paise") or 0)),
        },
    }
    if type_code in {"CRN", "DBN"}:
        source_number = _required_text(document.get("source_invoice_number"), "source invoice number", maximum=16)
        source_date = document.get("source_invoice_date")
        payload["RefDtls"] = {
            "PrecDocDtls": [{"InvNo": source_number, "InvDt": _irp_date(source_date, "source invoice date")}]
        }
    return payload


def _outer_value(payload: dict, name: str) -> Any:
    return payload.get(name) if name in payload else payload.get(name[:1].lower() + name[1:])


def _validate_outer_response(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="IRIS IRP returned an invalid response envelope")
    status = _outer_value(payload, "Status")
    if status != 1:
        errors = _outer_value(payload, "ErrorDetails")
        if not isinstance(errors, list):
            errors = []
        safe_errors = []
        for error in errors[:10]:
            if isinstance(error, dict):
                safe_errors.append({
                    "code": str(error.get("ErrorCode") or "")[:32],
                    "message": str(error.get("ErrorMessage") or "IRIS rejected the request")[:300],
                })
        raise HTTPException(
            status_code=422,
            detail={"code": "iris_irp_rejected", "errors": safe_errors or [{"message": "IRIS rejected the request"}]},
        )
    data = _outer_value(payload, "Data")
    if data is None:
        raise HTTPException(status_code=502, detail="IRIS IRP success response omitted Data")
    return data


def validate_irn_artifact(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="IRIS IRP returned invalid invoice data")
    irn = str(payload.get("Irn") or "")
    ack_number = str(payload.get("AckNo") or "")
    ack_at = str(payload.get("AckDt") or "")
    signed_invoice = str(payload.get("SignedInvoice") or "")
    signed_qr = str(payload.get("SignedQRCode") or "")
    status = str(payload.get("Status") or "")
    if not _IRN_RE.fullmatch(irn) or not ack_number.isdigit() or len(ack_number) > 32:
        raise HTTPException(status_code=502, detail="IRIS IRP returned an invalid IRN or acknowledgement")
    if not ack_at or len(ack_at) > 40 or status not in {"ACT", "CNL"}:
        raise HTTPException(status_code=502, detail="IRIS IRP returned invalid acknowledgement metadata")
    if not signed_invoice or not signed_qr or len(signed_invoice) > _MAX_SIGNED_ARTIFACT or len(signed_qr) > _MAX_SIGNED_ARTIFACT:
        raise HTTPException(status_code=502, detail="IRIS IRP returned missing or oversized signed artifacts")
    return {
        "irn": irn.lower(),
        "ack_number": ack_number,
        "ack_at_raw": ack_at,
        "status": status,
        "signed_invoice": signed_invoice,
        "signed_qr": signed_qr,
    }


def validate_cancel_artifact(payload: Any, expected_irn: str) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="IRIS IRP returned invalid cancellation data")
    irn = str(payload.get("Irn") or "")
    cancelled_at = str(payload.get("CancelDate") or "")
    if irn.lower() != expected_irn.lower() or not _IRN_RE.fullmatch(irn) or not cancelled_at or len(cancelled_at) > 40:
        raise HTTPException(status_code=502, detail="IRIS IRP returned invalid cancellation metadata")
    return {"irn": irn.lower(), "status": "CNL", "cancelled_at_raw": cancelled_at}


def irp_persistence_fields(
    artifact: dict,
    *,
    environment: str,
    cipher: Fernet,
    received_at: datetime | None = None,
) -> dict:
    """Stable persistence contract for an invoice/note's compliance artifact."""

    required = {"irn", "ack_number", "ack_at_raw", "status", "signed_invoice", "signed_qr"}
    if not required.issubset(artifact):
        raise HTTPException(status_code=500, detail="Incomplete IRP artifact cannot be persisted")
    return {
        "provider": "iris_irp6",
        "environment": environment,
        "irn": artifact["irn"],
        "ack_number": artifact["ack_number"],
        "ack_at_raw": artifact["ack_at_raw"],
        "status": artifact["status"],
        "signed_invoice_encrypted": cipher.encrypt(str(artifact["signed_invoice"]).encode()).decode(),
        "signed_qr_encrypted": cipher.encrypt(str(artifact["signed_qr"]).encode()).decode(),
        "received_at": received_at or datetime.now(timezone.utc),
        "schema_version": "1.1",
    }


class IrisIrpAdapter:
    """Mockable Core API adapter; it never logs credentials or plaintext data."""

    def __init__(
        self,
        *,
        config: IrisIrpConfig,
        transport: IrisIrpTransport,
        codec: IrisIrpCodec,
        credential_collection: Any,
        cipher: Fernet,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.base_url = validate_base_url(config.base_url, config.environment)
        if not config.client_id or not config.client_secret or not config.public_key_pem:
            raise HTTPException(status_code=503, detail="IRIS IRP client credentials are incomplete")
        self.transport = transport
        self.codec = codec
        self.credentials = credential_collection
        self.cipher = cipher
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self._auth_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _url(self, path: str) -> str:
        if path.startswith("/eicore/v1.03/Invoice/irn/"):
            irn = path.rsplit("/", 1)[-1]
            if not _IRN_RE.fullmatch(irn):
                raise HTTPException(status_code=422, detail="IRN must be 64 hexadecimal characters")
        elif path.startswith("/eivital/v1.04/Master/gstin/"):
            gstin = path.rsplit("/", 1)[-1]
            if not is_valid_gstin(gstin):
                raise HTTPException(status_code=422, detail="GSTIN is invalid")
        elif path not in _CORE_PATHS:
            raise HTTPException(status_code=500, detail="Unsupported IRIS IRP API path")
        return self.base_url + path

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict | None = None,
        params: dict[str, str] | None = None,
        retry_safe: bool,
    ) -> dict:
        attempts = 3 if retry_safe else 1
        for attempt in range(attempts):
            try:
                response = await self.transport.request(
                    method,
                    self._url(path),
                    headers=headers,
                    json=body,
                    params=params,
                )
            except (httpx.TransportError, TimeoutError, OSError) as exc:
                if not retry_safe:
                    raise IrisIrpOutcomeUnknown(
                        "IRIS mutating request outcome is unknown; reconcile before retrying"
                    ) from exc
                if attempt + 1 == attempts:
                    raise HTTPException(status_code=503, detail="IRIS IRP is temporarily unavailable") from exc
                await self.sleep(0.2 * (attempt + 1))
                continue
            status_code = int(getattr(response, "status_code", 0))
            if 300 <= status_code < 400:
                raise HTTPException(status_code=502, detail="IRIS IRP redirect was rejected")
            if status_code in {429, 502, 503, 504} and retry_safe and attempt + 1 < attempts:
                await self.sleep(0.2 * (attempt + 1))
                continue
            if status_code != 200:
                if not retry_safe and status_code >= 500:
                    raise IrisIrpOutcomeUnknown(
                        "IRIS mutating request outcome is unknown; reconcile before retrying"
                    )
                raise HTTPException(status_code=502, detail=f"IRIS IRP returned HTTP {status_code}")
            content_type = str(getattr(response, "headers", {}).get("content-type", ""))
            if content_type and "application/json" not in content_type.lower():
                raise HTTPException(status_code=502, detail="IRIS IRP returned an unexpected content type")
            try:
                payload = response.json()
            except Exception as exc:
                raise HTTPException(status_code=502, detail="IRIS IRP returned malformed JSON") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=502, detail="IRIS IRP returned an invalid response")
            return payload
        raise HTTPException(status_code=503, detail="IRIS IRP is temporarily unavailable")

    def _basic_headers(self, gstin: str) -> dict[str, str]:
        gstin = gstin.strip().upper()
        if not is_valid_gstin(gstin):
            raise HTTPException(status_code=422, detail="Seller GSTIN is invalid")
        return {
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "gstin": gstin,
            "accept": "application/json",
            "content-type": "application/json",
        }

    def _auth_headers(self, gstin: str, session: AuthSession) -> dict[str, str]:
        return {
            **self._basic_headers(gstin),
            "user_name": session.username,
            "AuthToken": session.auth_token,
        }

    async def _cached_session(self, org_id: str, gstin: str) -> AuthSession | None:
        document = await self.credentials.find_one({
            "org_id": org_id,
            "gstin": gstin,
            "environment": self.config.environment,
        })
        if not document:
            raise HTTPException(status_code=409, detail="IRP credentials are not configured for the seller GSTIN")
        expires = document.get("token_expires_at")
        if isinstance(expires, datetime) and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if not isinstance(expires, datetime) or expires <= self.now() + timedelta(minutes=5):
            return None
        try:
            return AuthSession(
                username=self.cipher.decrypt(document["username_encrypted"].encode()).decode(),
                auth_token=self.cipher.decrypt(document["auth_token_encrypted"].encode()).decode(),
                session_key=self.cipher.decrypt(document["session_key_encrypted"].encode()).decode(),
                expires_at=expires,
            )
        except (InvalidToken, KeyError, UnicodeDecodeError):
            return None

    async def _authenticate(self, org_id: str, gstin: str) -> AuthSession:
        cached = await self._cached_session(org_id, gstin)
        if cached:
            return cached
        key = (org_id, gstin)
        lock = self._auth_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await self._cached_session(org_id, gstin)
            if cached:
                return cached
            document = await self.credentials.find_one({
                "org_id": org_id,
                "gstin": gstin,
                "environment": self.config.environment,
            })
            if not document:
                raise HTTPException(status_code=409, detail="IRP credentials are not configured for the seller GSTIN")
            try:
                username = self.cipher.decrypt(document["username_encrypted"].encode()).decode()
                password = self.cipher.decrypt(document["password_encrypted"].encode()).decode()
            except (InvalidToken, KeyError, UnicodeDecodeError) as exc:
                raise HTTPException(status_code=503, detail="Stored IRP credentials cannot be decrypted") from exc
            try:
                envelope = self.codec.encrypt_auth(username, password, self.config.public_key_pem)
            except IrisIrpCodecUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if not envelope.data or len(envelope.data) > _MAX_ENCRYPTED_RESPONSE:
                raise HTTPException(status_code=503, detail="IRIS auth encryption returned invalid data")
            response = await self._request_json(
                "POST",
                "/eivital/v1.04/auth",
                headers=self._basic_headers(gstin),
                body={"Data": envelope.data},
                retry_safe=True,
            )
            response_data = _validate_outer_response(response)
            if not isinstance(response_data, dict):
                raise HTTPException(status_code=502, detail="IRIS auth response Data is invalid")
            session = self.codec.complete_auth(response_data, envelope.context, now=self.now())
            if (
                session.username != username
                or not session.auth_token
                or not session.session_key
                or len(session.auth_token) > 4096
                or len(session.session_key) > 4096
            ):
                raise HTTPException(status_code=502, detail="IRIS authentication returned an invalid session")
            expires_at = min(session.expires_at, self.now() + _TOKEN_CACHE_TTL)
            if expires_at <= self.now() + timedelta(minutes=5):
                raise HTTPException(status_code=502, detail="IRIS authentication returned an expired session")
            session = AuthSession(username, session.auth_token, session.session_key, expires_at)
            await self.credentials.update_one(
                {"org_id": org_id, "gstin": gstin, "environment": self.config.environment},
                {"$set": {
                    "auth_token_encrypted": self.cipher.encrypt(session.auth_token.encode()).decode(),
                    "session_key_encrypted": self.cipher.encrypt(session.session_key.encode()).decode(),
                    "token_expires_at": expires_at,
                    "verified_at": self.now(),
                    "auth_updated_at": self.now(),
                }},
            )
            return session

    def _decode_core(self, response: dict, session: AuthSession) -> dict:
        encrypted = _validate_outer_response(response)
        if not isinstance(encrypted, str) or not encrypted or len(encrypted) > _MAX_ENCRYPTED_RESPONSE:
            raise HTTPException(status_code=502, detail="IRIS IRP returned invalid encrypted Data")
        decoded = self.codec.decrypt_core(encrypted, session.session_key)
        if not isinstance(decoded, dict):
            raise HTTPException(status_code=502, detail="IRIS IRP decrypted Data is invalid")
        return decoded

    async def generate(
        self,
        org_id: str,
        document: dict,
        *,
        document_type: str | None = None,
        aato_10cr_reporting_limit_applies: bool,
        today: date | None = None,
    ) -> dict:
        payload = map_domestic_b2b_document(document, document_type=document_type)
        enforce_reporting_window(
            payload["DocDtls"]["Dt"],
            aato_10cr_reporting_limit_applies=aato_10cr_reporting_limit_applies,
            today=today,
        )
        gstin = payload["SellerDtls"]["Gstin"]
        session = await self._authenticate(org_id, gstin)
        encrypted = self.codec.encrypt_core(payload, session.session_key)
        if not encrypted or len(encrypted) > _MAX_ENCRYPTED_RESPONSE:
            raise HTTPException(status_code=503, detail="IRIS request encryption returned invalid data")
        response = await self._request_json(
            "POST",
            "/eicore/v1.03/Invoice",
            headers=self._auth_headers(gstin, session),
            body={"Data": encrypted},
            retry_safe=False,
        )
        return validate_irn_artifact(self._decode_core(response, session))

    async def get_gstin_details(self, org_id: str, seller_gstin: str, gstin: str) -> dict:
        gstin = gstin.strip().upper()
        if not is_valid_gstin(gstin):
            raise HTTPException(status_code=422, detail="Enter a valid GSTIN with checksum")
        session = await self._authenticate(org_id, seller_gstin.strip().upper())
        response = await self._request_json(
            "POST",
            f"/eivital/v1.04/Master/gstin/{gstin}",
            headers=self._auth_headers(seller_gstin, session),
            retry_safe=True,
        )
        return gstin_details_view(self._decode_core(response, session), gstin)

    async def get_by_irn(self, org_id: str, gstin: str, irn: str) -> dict:
        irn = irn.strip().lower()
        if not _IRN_RE.fullmatch(irn):
            raise HTTPException(status_code=422, detail="IRN must be 64 hexadecimal characters")
        session = await self._authenticate(org_id, gstin.strip().upper())
        response = await self._request_json(
            "GET",
            f"/eicore/v1.03/Invoice/irn/{irn}",
            headers=self._auth_headers(gstin, session),
            retry_safe=True,
        )
        return validate_irn_artifact(self._decode_core(response, session))

    async def get_by_document(
        self,
        org_id: str,
        gstin: str,
        *,
        document_type: Literal["INV", "CRN", "DBN"],
        document_number: str,
        document_date: str | date,
    ) -> dict:
        if document_type not in {"INV", "CRN", "DBN"}:
            raise HTTPException(status_code=422, detail="IRP document type is invalid")
        session = await self._authenticate(org_id, gstin.strip().upper())
        response = await self._request_json(
            "GET",
            "/eicore/v1.03/Invoice/irnbydocdetails",
            headers=self._auth_headers(gstin, session),
            params={
                "doctype": document_type,
                "docnum": _required_text(document_number, "document number", maximum=16),
                "docdate": _irp_date(document_date),
            },
            retry_safe=True,
        )
        return validate_irn_artifact(self._decode_core(response, session))

    async def cancel(
        self,
        org_id: str,
        gstin: str,
        irn: str,
        *,
        reason_code: Literal["1", "2", "3", "4"],
        remarks: str,
    ) -> dict:
        irn = irn.strip().lower()
        if not _IRN_RE.fullmatch(irn):
            raise HTTPException(status_code=422, detail="IRN must be 64 hexadecimal characters")
        if reason_code not in {"1", "2", "3", "4"}:
            raise HTTPException(status_code=422, detail="IRP cancellation reason is invalid")
        plaintext = {"Irn": irn, "CnlRsn": reason_code, "CnlRem": _required_text(remarks, "cancellation remarks")}
        session = await self._authenticate(org_id, gstin.strip().upper())
        encrypted = self.codec.encrypt_core(plaintext, session.session_key)
        if not encrypted or len(encrypted) > _MAX_ENCRYPTED_RESPONSE:
            raise HTTPException(status_code=503, detail="IRIS request encryption returned invalid data")
        response = await self._request_json(
            "POST",
            "/eicore/v1.03/Invoice/Cancel",
            headers=self._auth_headers(gstin, session),
            body={"Data": encrypted},
            retry_safe=False,
        )
        return validate_cancel_artifact(self._decode_core(response, session), irn)


def iris_irp_adapter(*, codec: IrisIrpCodec | None = None, transport: IrisIrpTransport | None = None) -> IrisIrpAdapter:
    """Construct the application adapter; default crypto remains fail closed."""

    require_iris_irp_configuration()
    return IrisIrpAdapter(
        config=IrisIrpConfig(
            environment=IRIS_IRP_ENV,
            base_url=IRIS_IRP_API_BASE_URL,
            client_id=IRIS_IRP_CLIENT_ID,
            client_secret=IRIS_IRP_CLIENT_SECRET,
            public_key_pem=IRIS_IRP_PUBLIC_KEY_PEM,
        ),
        transport=transport or HttpxIrisIrpTransport(),
        codec=codec or UnconfiguredIrisIrpCodec(),
        credential_collection=credentials,
        cipher=_fernet(),
    )


def gstin_details_view(value: dict, requested_gstin: str) -> dict:
    data: Any = value.get("Data", value) if isinstance(value, dict) else value
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="IRIS GSTIN response is invalid") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="IRIS GSTIN response is invalid")
    gstin = str(data.get("gstin") or data.get("Gstin") or requested_gstin).strip().upper()
    if gstin != requested_gstin or not is_valid_gstin(gstin):
        raise HTTPException(status_code=502, detail="IRIS returned GSTIN details for a different taxpayer")
    state_code = gstin[:2]
    state_name = str(data.get("stateCode") or data.get("Stcd") or GST_STATE_NAMES.get(state_code, "")).strip()
    if state_code not in GST_STATE_NAMES:
        raise HTTPException(status_code=502, detail="IRIS returned an invalid taxpayer state")
    parts = [str(data.get(key) or "").strip() for key in ("addrFlno", "addrBno", "addrBnm", "addrSt")]
    address = ", ".join(part for part in parts if part and part != ".")
    pincode = re.sub(r"\D", "", str(data.get("addrPncd") or data.get("Pin") or ""))
    return {
        "gstin": gstin,
        "legal_name": str(data.get("legalName") or data.get("LglNm") or "").strip()[:120],
        "trade_name": str(data.get("tradeName") or data.get("TrdNm") or "").strip()[:120],
        "address": address[:500],
        "location": str(data.get("addrLoc") or data.get("Loc") or "").strip()[:120],
        "pincode": pincode if len(pincode) == 6 else "",
        "state_code": state_code,
        "state_name": state_name or GST_STATE_NAMES[state_code],
        "taxpayer_type": str(data.get("txpType") or "").strip()[:80],
        "registration_date": str(data.get("dtReg") or "").strip()[:20],
        "deregistration_date": str(data.get("dtDReg") or "").strip()[:20],
        "source": "iris_irp",
    }
