import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.sdjwt import common, sd_jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwcrypto.jwk import JWK
from jwt.utils import base64url_encode
from pydantic import SecretStr
from test_payment_flow import AGENT_SECRET, MERCHANT_SECRET, NOW, delegation

from tesis_payment_provider.ap2 import PaymentAp2
from tesis_payment_provider.api import create_app
from tesis_payment_provider.config import Settings
from tesis_payment_provider.models import ConfirmPaymentRequest, DelegatePaymentRequest
from tesis_payment_provider.store import PaymentError, PaymentStore


def _key() -> bytes:
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def test_ap2_provider_verifies_mandate_before_token_and_signs_receipt(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    agent_key = JWK.from_pem(_key())
    merchant_pem = _key()
    provider_pem = _key()
    settings = Settings(
        payment_database_path=tmp_path / "payments.db",
        payment_agent_bearer_token=SecretStr(AGENT_SECRET),
        payment_merchant_bearer_token=SecretStr(MERCHANT_SECRET),
        payment_token_secret=SecretStr("token-secret-with-at-least-thirty-two-characters"),
        payment_ap2_required=True,
        payment_ap2_agent_jwk_json=SecretStr(agent_key.export_public()),
        payment_ap2_merchant_jwk_json=SecretStr(JWK.from_pem(merchant_pem).export_public()),
        payment_ap2_merchant_issuer="https://merchant.example.test",
        payment_ap2_private_key_pem=SecretStr(provider_pem.decode()),
    )
    client = TestClient(create_app(settings, clock=lambda: now))
    body = delegation()
    allowance = body["allowance"]
    assert isinstance(allowance, dict)
    allowance["expires_at"] = (now + timedelta(minutes=5)).isoformat()
    checkout_jwt = jwt.encode(
        {
            "iss": "https://merchant.example.test",
            "aud": "ap2-shopping-agent",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "jti": "checkout-1",
            "checkout": {
                "id": "chk_test_1",
                "status": "ready_for_payment",
                "currency": "usd",
                "totals": [{"type": "total", "amount": 62000}],
            },
        },
        merchant_pem,
        algorithm="ES256",
    )
    transaction_id = base64url_encode(hashlib.sha256(checkout_jwt.encode()).digest()).decode()
    mandate = PaymentMandate(
        transaction_id=transaction_id,
        payee=Merchant(id="tesis_merchant", name="Merchant"),
        payment_amount=Amount(amount=62000, currency="USD"),
        payment_instrument=PaymentInstrument(id="tesis_sandbox", type="sandbox_token"),
        iat=int(now.timestamp()),
        exp=int((now + timedelta(minutes=5)).timestamp()),
    )
    token = sd_jwt.create(mandate, agent_key).sd_jwt_issuance
    body["ap2_checkout_jwt"] = checkout_jwt
    body["ap2_payment_mandate"] = token
    headers = {
        "Authorization": f"Bearer {AGENT_SECRET}",
        "API-Version": "2026-04-17",
        "Idempotency-Key": "delegate-ap2",
    }

    missing = client.post("/agentic_commerce/delegate_payment", headers=headers, json=delegation())
    assert missing.status_code == 422
    foreign_key = JWK.from_pem(_key())
    foreign_body = {
        **body,
        "ap2_payment_mandate": sd_jwt.create(mandate, foreign_key).sd_jwt_issuance,
    }
    rejected = client.post(
        "/agentic_commerce/delegate_payment",
        headers={**headers, "Idempotency-Key": "foreign-signature"},
        json=foreign_body,
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "invalid_payment_mandate"
    long_lived = mandate.model_copy(update={"exp": int((now + timedelta(hours=1)).timestamp())})
    rejected = client.post(
        "/agentic_commerce/delegate_payment",
        headers={**headers, "Idempotency-Key": "long-lived"},
        json={**body, "ap2_payment_mandate": sd_jwt.create(long_lived, agent_key).sd_jwt_issuance},
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "invalid_payment_mandate"
    changed = {**body, "allowance": {**allowance, "max_amount": 62001}}
    rejected = client.post(
        "/agentic_commerce/delegate_payment",
        headers={**headers, "Idempotency-Key": "changed-amount"},
        json=changed,
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "invalid_payment_mandate"

    delegated = client.post("/agentic_commerce/delegate_payment", headers=headers, json=body)
    assert delegated.status_code == 201, delegated.text
    payment_token = delegated.json()["id"]
    confirmed = client.post(
        "/payments/confirm",
        headers={
            "Authorization": f"Bearer {MERCHANT_SECRET}",
            "Idempotency-Key": "confirm-ap2",
        },
        json={
            "token": payment_token,
            "merchant_id": "tesis_merchant",
            "checkout_session_id": "chk_test_1",
            "amount": 62000,
            "currency": "usd",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    receipt_jwt = confirmed.json()["payment_receipt_jwt"]
    provider_jwk = client.get("/.well-known/ap2/jwks.json").json()["keys"][0]
    claims = jwt.decode(
        receipt_jwt,
        jwt.PyJWK.from_dict(provider_jwk).key,
        algorithms=["ES256"],
        issuer="tesis_payment_provider",
        options={"verify_aud": False},
    )
    assert claims["status"] == "Success"
    assert claims["reference"] == common.compute_sd_hash(common.parse_token(token))
    assert claims["payment_id"] == confirmed.json()["id"]
    assert (
        client.post(
            "/agentic_commerce/payment_receipt",
            headers={"Authorization": f"Bearer {AGENT_SECRET}"},
            json={"token": payment_token},
        ).json()["result"]["payment_receipt_jwt"]
        == receipt_jwt
    )
    database = (tmp_path / "payments.db").read_bytes()
    assert checkout_jwt.encode() not in database
    assert token.encode() not in database
    assert payment_token.encode() not in database


def test_ap2_mode_rejects_tokens_issued_before_mandate_enforcement(tmp_path: Path) -> None:
    database = tmp_path / "previous.db"
    secret = SecretStr("token-secret-with-at-least-thirty-two-characters")
    legacy = PaymentStore(database, secret, "tesis_merchant", clock=lambda: NOW)
    legacy.initialize()
    token = legacy.delegate(
        DelegatePaymentRequest.model_validate(delegation()), idempotency_key="old-token"
    ).id
    protected = PaymentStore(
        database,
        secret,
        "tesis_merchant",
        clock=lambda: NOW,
        ap2=PaymentAp2(
            agent_jwk_json=JWK.from_pem(_key()).export_public(),
            merchant_jwk_json=JWK.from_pem(_key()).export_public(),
            merchant_issuer="https://merchant.example.test",
            private_key_pem=_key().decode(),
        ),
    )
    with pytest.raises(PaymentError) as replay:
        protected.delegate(
            DelegatePaymentRequest.model_validate(delegation()), idempotency_key="old-token"
        )
    assert replay.value.code == "invalid_payment_mandate"
    with pytest.raises(PaymentError) as rejected:
        protected.confirm(
            ConfirmPaymentRequest(
                token=SecretStr(token),
                merchant_id="tesis_merchant",
                checkout_session_id="chk_test_1",
                amount=62000,
                currency="usd",
            ),
            idempotency_key="confirm-old-token",
        )
    assert rejected.value.code == "invalid_payment_mandate"


def test_ap2_configuration_rejects_private_trust_keys() -> None:
    agent_key = JWK.from_pem(_key())
    with pytest.raises(ValueError, match="public P-256"):
        PaymentAp2(
            agent_jwk_json=agent_key.export(),
            merchant_jwk_json=JWK.from_pem(_key()).export_public(),
            merchant_issuer="https://merchant.example.test",
            private_key_pem=_key().decode(),
        )
