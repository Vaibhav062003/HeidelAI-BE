import asyncio
import os
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGODB_CLUSTER", "iris_irp_contract_test")

from services.iris_irp_service import (  # noqa: E402
    AuthEnvelope,
    AuthSession,
    IrisIrpAdapter,
    IrisIrpCodecUnavailable,
    IrisIrpConfig,
    IrisIrpOutcomeUnknown,
    UnconfiguredIrisIrpCodec,
    enforce_reporting_window,
    gstin_details_view,
    irp_persistence_fields,
    map_domestic_b2b_document,
    validate_base_url,
)


NOW = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)
IRN = "a" * 64


def invoice_document() -> dict:
    return {
        "document_kind": "invoice",
        "invoice_number": "INV/26-27/000001",
        "invoice_date": "2026-09-10",
        "currency": "INR",
        "supply_category": "domestic",
        "reverse_charge": False,
        "place_of_supply_state_code": "29",
        "seller": {
            "name": "Seller Private Limited",
            "legal_name": "Seller Private Limited",
            "trade_name": "Seller",
            "gstin": "27BCKPS4201C1ZV",
            "address": "5 Main Road",
            "location": "Mumbai",
            "pincode": "400001",
            "state_code": "27",
        },
        "customer": {
            "name": "Buyer Private Limited",
            "legal_name": "Buyer Private Limited",
            "gstin": "29AAACB2894G1ZJ",
            "address": "7 Market Road",
            "location": "Bengaluru",
            "pincode": "560001",
            "state_code": "29",
        },
        "items": [{
            "description": "Consulting",
            "supply_type": "services",
            "hsn_sac": "998311",
            "quantity": "2",
            "uqc": "NOS",
            "taxable_before_discount_paise": 20_000,
            "discount_paise": 0,
            "taxable_value_paise": 20_000,
            "gst_rate_bps": 1800,
            "cgst_paise": 0,
            "sgst_paise": 0,
            "igst_paise": 3_600,
            "total_paise": 23_600,
        }],
        "taxable_value_paise": 20_000,
        "discount_paise": 0,
        "cgst_paise": 0,
        "sgst_paise": 0,
        "igst_paise": 3_600,
        "total_paise": 23_600,
    }


class FakeCollection:
    def __init__(self, document: dict):
        self.document = document
        self.updates = []

    async def find_one(self, query):
        if all(self.document.get(key) == value for key, value in query.items()):
            return deepcopy(self.document)
        return None

    async def update_one(self, query, update):
        assert all(self.document.get(key) == value for key, value in query.items())
        self.document.update(deepcopy(update["$set"]))
        self.updates.append(deepcopy(update))


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self):
        return deepcopy(self.payload)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeCodec:
    def __init__(self):
        self.encrypted_payloads = []

    def encrypt_auth(self, username, password, public_key_pem):
        assert (username, password, public_key_pem) == ("taxuser", "password1", "PUBLIC")
        return AuthEnvelope("AUTH-CIPHERTEXT", "APP-KEY")

    def complete_auth(self, response_data, context, *, now):
        assert response_data["AuthToken"] == "TOKEN" and context == "APP-KEY"
        return AuthSession("taxuser", "TOKEN", "SEK", now + timedelta(hours=6))

    def encrypt_core(self, payload, session_key):
        assert session_key == "SEK"
        self.encrypted_payloads.append(deepcopy(payload))
        return "CORE-CIPHERTEXT"

    def decrypt_core(self, data, session_key):
        assert session_key == "SEK"
        if data == "CANCELLED":
            return {"Irn": IRN, "CancelDate": "2026-09-17 13:30:00"}
        return {
            "AckNo": 123456789012345,
            "AckDt": "2026-09-17 13:29:00",
            "Irn": IRN,
            "SignedInvoice": "signed-invoice",
            "SignedQRCode": "signed-qr",
            "Status": "ACT",
        }


def adapter(responses, *, token_cached=False):
    cipher = Fernet(Fernet.generate_key())
    document = {
        "org_id": "org-1",
        "gstin": "27BCKPS4201C1ZV",
        "environment": "sandbox",
        "username_encrypted": cipher.encrypt(b"taxuser").decode(),
        "password_encrypted": cipher.encrypt(b"password1").decode(),
    }
    if token_cached:
        document.update({
            "auth_token_encrypted": cipher.encrypt(b"TOKEN").decode(),
            "session_key_encrypted": cipher.encrypt(b"SEK").decode(),
            "token_expires_at": NOW + timedelta(hours=1),
        })
    collection = FakeCollection(document)
    transport = FakeTransport(responses)
    codec = FakeCodec()
    service = IrisIrpAdapter(
        config=IrisIrpConfig(
            environment="sandbox",
            base_url="https://api.sandbox.core.irisirp.com",
            client_id="client",
            client_secret="secret",
            public_key_pem="PUBLIC",
        ),
        transport=transport,
        codec=codec,
        credential_collection=collection,
        cipher=cipher,
        now=lambda: NOW,
        sleep=lambda _: asyncio.sleep(0),
    )
    return service, transport, codec, collection


def test_strict_official_origin_and_fail_closed_crypto():
    assert validate_base_url("https://api.sandbox.core.irisirp.com", "sandbox") == "https://api.sandbox.core.irisirp.com"
    for value in (
        "http://api.sandbox.core.irisirp.com",
        "https://evil.irisirp.com",
        "https://api.sandbox.core.irisirp.com.evil.example",
        "https://api.sandbox.core.irisirp.com/path",
    ):
        with pytest.raises(HTTPException):
            validate_base_url(value, "sandbox")
    with pytest.raises(HTTPException):
        validate_base_url("https://api.sandbox.core.irisirp.com", "production")
    with pytest.raises(IrisIrpCodecUnavailable):
        UnconfiguredIrisIrpCodec().encrypt_core({}, "sek")


def test_mapping_and_reporting_window_contract():
    mapped = map_domestic_b2b_document(invoice_document())
    assert mapped["DocDtls"] == {"Typ": "INV", "No": "INV/26-27/000001", "Dt": "10/09/2026"}
    assert mapped["TranDtls"]["SupTyp"] == "B2B"
    assert mapped["ItemList"][0]["IsServc"] == "Y"
    assert mapped["ItemList"][0]["TotItemVal"] == 236
    assert mapped["ValDtls"]["TotInvVal"] == 236
    enforce_reporting_window("18/08/2026", aato_10cr_reporting_limit_applies=True, today=date(2026, 9, 17))
    with pytest.raises(HTTPException) as error:
        enforce_reporting_window("17/08/2026", aato_10cr_reporting_limit_applies=True, today=date(2026, 9, 17))
    assert error.value.status_code == 409


def test_gstin_details_are_normalized_for_invoice_autofill():
    details = gstin_details_view({
        "gstin": "27BCKPS4201C1ZV",
        "tradeName": "Seller",
        "legalName": "Seller Private Limited",
        "addrBnm": "Aries Compound",
        "addrBno": "106/3",
        "addrFlno": ".",
        "addrSt": "Main Road",
        "addrLoc": "Mumbai",
        "stateCode": "Maharashtra",
        "addrPncd": "400001",
        "txpType": "Regular",
    }, "27BCKPS4201C1ZV")
    assert details["state_code"] == "27"
    assert details["address"] == "106/3, Aries Compound, Main Road"
    assert details["pincode"] == "400001"
    assert details["source"] == "iris_irp"


@pytest.mark.asyncio
async def test_generate_contract_auth_cache_headers_and_persistence():
    service, transport, codec, collection = adapter([
        FakeResponse({"Status": 1, "Data": {"AuthToken": "TOKEN", "Sek": "opaque"}}),
        FakeResponse({"Status": 1, "Data": "GENERATED"}),
    ])
    artifact = await service.generate(
        "org-1",
        invoice_document(),
        aato_10cr_reporting_limit_applies=True,
        today=date(2026, 9, 17),
    )
    assert artifact["irn"] == IRN and artifact["signed_qr"] == "signed-qr"
    assert [call[1] for call in transport.calls] == [
        "https://api.sandbox.core.irisirp.com/eivital/v1.04/auth",
        "https://api.sandbox.core.irisirp.com/eicore/v1.03/Invoice",
    ]
    assert transport.calls[0][2]["headers"]["gstin"] == "27BCKPS4201C1ZV"
    assert transport.calls[1][2]["headers"]["AuthToken"] == "TOKEN"
    assert transport.calls[1][2]["json"] == {"Data": "CORE-CIPHERTEXT"}
    assert codec.encrypted_payloads[0]["DocDtls"]["Typ"] == "INV"
    assert collection.document["token_expires_at"] == NOW + timedelta(hours=5, minutes=45)
    persisted = irp_persistence_fields(artifact, environment="sandbox", cipher=service.cipher, received_at=NOW)
    assert "signed_qr" not in persisted and persisted["schema_version"] == "1.1"
    assert service.cipher.decrypt(persisted["signed_qr_encrypted"].encode()) == b"signed-qr"


@pytest.mark.asyncio
async def test_get_gstin_details_uses_documented_master_endpoint():
    service, transport, codec, _ = adapter([
        FakeResponse({"Status": 1, "Data": "GSTIN-DETAILS"}),
    ], token_cached=True)
    codec.decrypt_core = lambda data, session_key: {
        "gstin": "29AAACB2894G1ZJ", "legalName": "Buyer Limited", "tradeName": "Buyer",
        "addrBno": "7", "addrBnm": "Market Road", "addrLoc": "Bengaluru",
        "stateCode": "Karnataka", "addrPncd": "560001", "txpType": "Regular",
    }
    details = await service.get_gstin_details("org-1", "27BCKPS4201C1ZV", "29AAACB2894G1ZJ")
    assert details["legal_name"] == "Buyer Limited" and details["state_code"] == "29"
    assert transport.calls[0][0] == "POST"
    assert transport.calls[0][1].endswith("/eivital/v1.04/Master/gstin/29AAACB2894G1ZJ")


@pytest.mark.asyncio
async def test_get_retries_but_mutation_is_never_blindly_retried():
    service, transport, _, _ = adapter([
        FakeResponse({}, status_code=503),
        FakeResponse({"Status": 1, "Data": "GENERATED"}),
    ], token_cached=True)
    result = await service.get_by_irn("org-1", "27BCKPS4201C1ZV", IRN)
    assert result["irn"] == IRN and len(transport.calls) == 2

    service, transport, _, _ = adapter([httpx.ReadTimeout("timeout")], token_cached=True)
    with pytest.raises(IrisIrpOutcomeUnknown):
        await service.cancel(
            "org-1", "27BCKPS4201C1ZV", IRN, reason_code="1", remarks="Duplicate invoice"
        )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_cancel_uses_documented_encrypted_contract():
    service, transport, codec, _ = adapter([
        FakeResponse({"status": 1, "data": "CANCELLED", "ErrorDetails": None}),
    ], token_cached=True)
    result = await service.cancel(
        "org-1", "27BCKPS4201C1ZV", IRN, reason_code="2", remarks="Data entry error"
    )
    assert result == {"irn": IRN, "status": "CNL", "cancelled_at_raw": "2026-09-17 13:30:00"}
    assert codec.encrypted_payloads == [{"Irn": IRN, "CnlRsn": "2", "CnlRem": "Data entry error"}]
    assert transport.calls[0][1].endswith("/eicore/v1.03/Invoice/Cancel")
