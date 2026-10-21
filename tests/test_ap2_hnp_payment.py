"""Human Not Present payment chains (profile `ap2-hnp/v1`, «Verificación en el PSP»).

The chains are built here with the AP2 SDK and keys generated per test, following the
profile; they are not the golden fixtures of the agent's issuer (HNP-04).
"""

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import OpenCheckoutMandate
from ap2.sdk.generated.open_payment_mandate import OpenPaymentMandate
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.payment_receipt import PaymentReceiptError, PaymentReceiptSuccess
from ap2.sdk.mandate import MandateClient
from ap2.sdk.sdjwt import common, kb_sd_jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwcrypto.jwk import JWK
from jwt.utils import base64url_encode
from pydantic import BaseModel, ConfigDict, SecretStr
from test_payment_flow import AGENT_SECRET, MERCHANT_SECRET, delegation

from tesis_payment_provider.ap2 import PaymentAp2
from tesis_payment_provider.api import create_app
from tesis_payment_provider.config import Settings
from tesis_payment_provider.models import DelegatePaymentRequest
from tesis_payment_provider.store import PaymentError, PaymentStore

MERCHANT_ISSUER = "https://merchant.example.test"
PSP_AUDIENCE = "tesis_payment_provider"
MERCHANT = {"id": "tesis_merchant", "name": "Tesis Commerce Merchant"}
INSTRUMENT = {"id": "tesis_sandbox", "type": "sandbox_token"}
TOTAL = 62000
HEADERS = {
    "Authorization": f"Bearer {AGENT_SECRET}",
    "API-Version": "2026-04-17",
    "Idempotency-Key": "op_1:payment",
}


def _pem() -> bytes:
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _sha256(value: str) -> str:
    return base64url_encode(hashlib.sha256(value.encode()).digest()).decode()


class _RawClaims(BaseModel):
    """Signs claims the SDK models would reject or silently drop."""

    model_config = ConfigDict(extra="allow")


class Hnp:
    """Builds the two chains of one Human Not Present purchase, as the profile describes."""

    def __init__(self, now: int) -> None:
        self.now = now
        self.provider = JWK.from_pem(_pem())  # AGENT_AP2_PRIVATE_KEY_PEM: signs the opens
        self.agent = JWK.from_pem(_pem())  # AGENT_HNP_AGENT_KEY_PEM: closes them
        self.merchant_pem = _pem()
        self.receipt_pem = _pem()
        self.client = MandateClient()
        self.checkout_jwt = self.sign_checkout()
        self.open_checkout = self.sign_open_checkout()

    @property
    def cnf(self) -> dict[str, Any]:
        return {"jwk": json.loads(self.agent.export_public())}

    def sign_checkout(self, *, key: bytes | None = None, jti: str = "checkout-1") -> str:
        return jwt.encode(
            {
                "iss": MERCHANT_ISSUER,
                "aud": "ap2-shopping-agent",
                "iat": self.now,
                "exp": self.now + 300,
                "jti": jti,
                "checkout": {
                    "id": "chk_test_1",
                    "status": "ready_for_payment",
                    "currency": "cop",
                    "totals": [
                        {"type": "subtotal", "amount": TOTAL},
                        {"type": "total", "amount": TOTAL},
                    ],
                },
                "checkout_revision": 1,
            },
            key or self.merchant_pem,
            algorithm="ES256",
        )

    def sign_open_checkout(self) -> str:
        mandate = OpenCheckoutMandate.model_validate(
            {
                "constraints": [
                    {
                        "type": "checkout.allowed_merchants",
                        "allowed": [{**MERCHANT, "website": MERCHANT_ISSUER}],
                    },
                    {
                        "type": "checkout.line_items",
                        "items": [
                            {
                                "id": "line_1",
                                "quantity": 1,
                                "acceptable_items": [{"id": "var_1", "title": "Producto"}],
                            }
                        ],
                    },
                ],
                "cnf": self.cnf,
                "iat": self.now,
                "exp": self.now + 3600,
            }
        )
        return self.client.create([mandate], self.provider)

    def open_payment_claims(self, **changes: Any) -> dict[str, Any]:
        claims: dict[str, Any] = {
            "vct": "mandate.payment.open.1",
            "constraints": [
                {"type": "payment.amount_range", "currency": "COP", "max": 70000},
                {
                    "type": "payment.allowed_payees",
                    "allowed": [{**MERCHANT, "website": MERCHANT_ISSUER}],
                },
                {"type": "payment.allowed_payment_instruments", "allowed": [INSTRUMENT]},
                {
                    "type": "payment.reference",
                    "conditional_transaction_id": common.compute_issuer_jwt_hash(
                        common.parse_token(self.open_checkout)
                    ),
                },
            ],
            "cnf": self.cnf,
            "iat": self.now,
            "exp": self.now + 3600,
        }
        return {**claims, **changes}

    def sign_open_payment(self, claims: dict[str, Any] | None = None, *, raw: bool = False) -> str:
        claims = claims or self.open_payment_claims()
        mandate = _RawClaims(**claims) if raw else OpenPaymentMandate.model_validate(claims)
        return self.client.create([mandate], self.provider)

    def close_payment(
        self,
        open_token: str | None = None,
        *,
        holder: JWK | None = None,
        aud: str = PSP_AUDIENCE,
        nonce: str | None = None,
        hash_mode: Any = "sd_hash",
        **changes: Any,
    ) -> str:
        closed = PaymentMandate.model_validate(
            {
                "transaction_id": _sha256(self.checkout_jwt),
                "payee": MERCHANT,
                "payment_amount": {"amount": TOTAL, "currency": "COP"},
                "payment_instrument": INSTRUMENT,
                "iat": self.now,
                "exp": self.now + 300,
                **changes,
            }
        )
        return self.client.present(
            holder or self.agent,
            open_token or self.sign_open_payment(),
            [closed],
            aud=aud,
            nonce=nonce or _sha256(self.checkout_jwt),
            hash_mode=hash_mode,
        )

    def close_checkout(self, open_token: str | None = None, **changes: Any) -> str:
        closed = CheckoutMandate.model_validate(
            {
                "checkout_jwt": self.checkout_jwt,
                "checkout_hash": _sha256(self.checkout_jwt),
                "iat": self.now,
                "exp": self.now + 300,
                **changes,
            }
        )
        return self.client.present(
            self.agent,
            open_token or self.open_checkout,
            [closed],
            aud=MERCHANT_ISSUER,
            nonce=_sha256(self.checkout_jwt),
        )

    def body(self, **changes: Any) -> dict[str, Any]:
        body = delegation()
        allowance = body["allowance"]
        assert isinstance(allowance, dict)
        allowance.update(
            currency="cop",
            max_amount=TOTAL,
            expires_at=datetime.fromtimestamp(self.now + 300, UTC).isoformat(),
        )
        body["ap2_checkout_jwt"] = self.checkout_jwt
        body["ap2_payment_mandate"] = self.close_payment()
        body["ap2_checkout_mandate"] = self.close_checkout()
        return {**body, **changes}

    def ap2(self, clock: Callable[[], datetime] | None = None) -> PaymentAp2:
        return PaymentAp2(
            agent_jwk_json=self.provider.export_public(),
            merchant_jwk_json=JWK.from_pem(self.merchant_pem).export_public(),
            merchant_issuer=MERCHANT_ISSUER,
            private_key_pem=self.receipt_pem.decode(),
            clock=clock,
        )

    def settings(self, path: Path, *, ap2: bool = True) -> Settings:
        settings = Settings(
            payment_database_path=path,
            payment_agent_bearer_token=SecretStr(AGENT_SECRET),
            payment_merchant_bearer_token=SecretStr(MERCHANT_SECRET),
            payment_token_secret=SecretStr("token-secret-with-at-least-thirty-two-characters"),
        )
        if not ap2:
            return settings
        return settings.model_copy(
            update={
                "payment_ap2_required": True,
                "payment_ap2_agent_jwk_json": SecretStr(self.provider.export_public()),
                "payment_ap2_merchant_jwk_json": SecretStr(
                    JWK.from_pem(self.merchant_pem).export_public()
                ),
                "payment_ap2_merchant_issuer": MERCHANT_ISSUER,
                "payment_ap2_private_key_pem": SecretStr(self.receipt_pem.decode()),
            }
        )


@pytest.fixture
def hnp() -> Hnp:
    return Hnp(int(time.time()))


@pytest.fixture
def client(hnp: Hnp, tmp_path: Path) -> TestClient:
    now = datetime.fromtimestamp(hnp.now, UTC)
    return TestClient(create_app(hnp.settings(tmp_path / "payments.db"), clock=lambda: now))


def _rows(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT COUNT(*) FROM delegations").fetchone()[0]


def _foreign_sd_hash(hnp: Hnp) -> str:
    """A closed payment signed by the Agent Key but bound to another open mandate."""
    other = hnp.close_payment(hnp.sign_open_payment(hnp.open_payment_claims(exp=hnp.now + 1800)))
    return hnp.sign_open_payment() + "~~" + other.split("~~")[1]


def _spoofed_kid(hnp: Hnp) -> str:
    """An open signed by another key that claims the pinned provider `kid`."""
    impostor = JWK(
        **{**json.loads(JWK.from_pem(_pem()).export()), "kid": hnp.provider.thumbprint()}
    )
    return hnp.close_payment(
        hnp.client.create([OpenPaymentMandate.model_validate(hnp.open_payment_claims())], impostor)
    )


def _other_checkout(hnp: Hnp) -> str:
    """A closed checkout that carries another (validly signed) checkout JWT under the right hash."""
    return hnp.close_checkout(checkout_jwt=hnp.sign_checkout(jti="checkout-2"))


def _other_session(hnp: Hnp) -> dict[str, Any]:
    body = hnp.body()
    body["allowance"] = {**body["allowance"], "checkout_session_id": "chk_other"}
    return body


def _closed_checkout(hnp: Hnp) -> CheckoutMandate:
    return CheckoutMandate(
        checkout_jwt=hnp.checkout_jwt,
        checkout_hash=_sha256(hnp.checkout_jwt),
        iat=hnp.now,
        exp=hnp.now + 300,
    )


def _constraints(hnp: Hnp, *extra: dict[str, Any], drop: str | None = None) -> str:
    claims = hnp.open_payment_claims()
    constraints = [c for c in claims["constraints"] if c["type"] != drop]
    return hnp.close_payment(
        hnp.sign_open_payment({**claims, "constraints": [*constraints, *extra]}, raw=True)
    )


def _amount_range_with(hnp: Hnp, **extra: Any) -> str:
    claims = hnp.open_payment_claims()
    constraints = [
        {**c, **extra} if c["type"] == "payment.amount_range" else c for c in claims["constraints"]
    ]
    return hnp.close_payment(
        hnp.sign_open_payment({**claims, "constraints": constraints}, raw=True)
    )


ALTERATIONS: dict[str, tuple[Callable[[Hnp], dict[str, Any]], str]] = {
    # invalid_mandate: the closed violates a supported constraint or the checkout binding.
    "total_out_of_range": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment(
                h.sign_open_payment(
                    h.open_payment_claims(
                        constraints=[
                            {"type": "payment.amount_range", "currency": "COP", "max": TOTAL - 1},
                            *h.open_payment_claims()["constraints"][1:],
                        ]
                    )
                )
            )
        ),
        "invalid_mandate",
    ),
    "payee": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment(payee={"id": "other_merchant", "name": "Other"})
        ),
        "invalid_mandate",
    ),
    "instrument": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment(
                payment_instrument={"id": "other_instrument", "type": "sandbox_token"}
            )
        ),
        "invalid_mandate",
    ),
    "reference_to_another_open_checkout": (
        lambda h: h.body(
            ap2_checkout_mandate=h.close_checkout(h.sign_open_checkout()),
        ),
        "invalid_mandate",
    ),
    "transaction_id": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment(transaction_id=_sha256(h.sign_checkout(jti="x")))
        ),
        "invalid_mandate",
    ),
    "checkout_jwt_of_another_checkout": (
        lambda h: h.body(ap2_checkout_mandate=_other_checkout(h)),
        "invalid_mandate",
    ),
    "checkout_hash": (
        lambda h: h.body(ap2_checkout_mandate=h.close_checkout(checkout_hash=_sha256("other"))),
        "invalid_mandate",
    ),
    "amount_differs_from_allowance": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment(
                payment_amount={"amount": TOTAL - 1000, "currency": "COP"}
            )
        ),
        "invalid_mandate",
    ),
    "checkout_of_another_session": (_other_session, "invalid_mandate"),
    "closed_outlives_profile": (
        lambda h: h.body(ap2_payment_mandate=h.close_payment(exp=h.now + 301)),
        "invalid_mandate",
    ),
    # invalid_credential: signature, root of trust, binding, aud/nonce, typ/vct, time.
    "closed_with_another_key": (
        lambda h: h.body(ap2_payment_mandate=h.close_payment(holder=JWK.from_pem(_pem()))),
        "invalid_credential",
    ),
    "foreign_sd_hash": (
        lambda h: h.body(ap2_payment_mandate=_foreign_sd_hash(h)),
        "invalid_credential",
    ),
    "issuer_jwt_hash_binding": (
        lambda h: h.body(ap2_payment_mandate=h.close_payment(hash_mode="issuer_jwt_hash")),
        "invalid_credential",
    ),
    "root_from_another_provider": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment(
                h.client.create(
                    [OpenPaymentMandate.model_validate(h.open_payment_claims())],
                    JWK.from_pem(_pem()),
                )
            )
        ),
        "invalid_credential",
    ),
    "root_kid_spoofed": (
        lambda h: h.body(ap2_payment_mandate=_spoofed_kid(h)),
        "invalid_credential",
    ),
    "merchant_audience": (
        lambda h: h.body(ap2_payment_mandate=h.close_payment(aud=MERCHANT_ISSUER)),
        "invalid_credential",
    ),
    "nonce": (
        lambda h: h.body(ap2_payment_mandate=h.close_payment(nonce="other")),
        "invalid_credential",
    ),
    "checkout_vct_in_payment_slot": (
        lambda h: h.body(
            ap2_payment_mandate=h.client.present(
                h.agent,
                h.open_checkout,
                [_closed_checkout(h)],
                aud=PSP_AUDIENCE,
                nonce=_sha256(h.checkout_jwt),
            )
        ),
        "invalid_credential",
    ),
    "closed_expired_within_sdk_skew": (
        lambda h: h.body(ap2_payment_mandate=h.close_payment(iat=h.now - 200, exp=h.now - 1)),
        "invalid_credential",
    ),
    "closed_issued_in_the_future": (
        lambda h: h.body(ap2_payment_mandate=h.close_payment(iat=h.now + 120, exp=h.now + 300)),
        "invalid_credential",
    ),
    "open_expired": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment(
                h.sign_open_payment(h.open_payment_claims(exp=h.now - 1))
            )
        ),
        "invalid_credential",
    ),
    "open_without_exp": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment(
                h.sign_open_payment(h.open_payment_claims(exp=None))
            )
        ),
        "invalid_credential",
    ),
    "checkout_chain_closed_with_another_key": (
        lambda h: h.body(
            ap2_checkout_mandate=h.close_checkout().split("~~")[0]
            + "~~"
            + h.client.present(
                JWK.from_pem(_pem()),
                h.open_checkout,
                [_closed_checkout(h)],
                aud=MERCHANT_ISSUER,
                nonce=_sha256(h.checkout_jwt),
            ).split("~~")[1]
        ),
        "invalid_credential",
    ),
    "three_segments": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment() + "~~" + h.close_payment().split("~~")[1]
        ),
        "invalid_credential",
    ),
    "checkout_jwt_from_another_merchant_key": (
        lambda h: _rebuilt_for(h, h.sign_checkout(key=_pem())),
        "invalid_credential",
    ),
    # unresolved_constraint: outside the profile; the agent escalates.
    "unknown_constraint": (
        lambda h: h.body(
            ap2_payment_mandate=_constraints(h, {"type": "payment.loyalty_points", "max": 1})
        ),
        "unresolved_constraint",
    ),
    "ap2_constraint_outside_profile": (
        lambda h: h.body(
            ap2_payment_mandate=_constraints(
                h, {"type": "payment.budget", "max": 1000.0, "currency": "COP"}
            )
        ),
        "unresolved_constraint",
    ),
    "unknown_property_in_known_constraint": (
        lambda h: h.body(ap2_payment_mandate=_amount_range_with(h, per_item=True)),
        "unresolved_constraint",
    ),
    "missing_reference_constraint": (
        lambda h: h.body(ap2_payment_mandate=_constraints(h, drop="payment.reference")),
        "unresolved_constraint",
    ),
    "open_with_preset_payee": (
        lambda h: h.body(
            ap2_payment_mandate=h.close_payment(
                h.sign_open_payment(h.open_payment_claims(payee=MERCHANT))
            )
        ),
        "unresolved_constraint",
    ),
    "missing_checkout_mandate": (
        lambda h: {k: v for k, v in h.body().items() if k != "ap2_checkout_mandate"},
        "unresolved_constraint",
    ),
}


def _rebuilt_for(hnp: Hnp, checkout_jwt: str) -> dict[str, Any]:
    hnp.checkout_jwt = checkout_jwt
    return hnp.body()


def test_valid_chain_issues_token_and_receipt_binds_the_closed_payment(
    hnp: Hnp, client: TestClient, tmp_path: Path
) -> None:
    body = hnp.body()
    delegated = client.post("/agentic_commerce/delegate_payment", headers=HEADERS, json=body)
    assert delegated.status_code == 201, delegated.text
    token = delegated.json()["id"]
    assert token.startswith("vt_")

    replay = client.post("/agentic_commerce/delegate_payment", headers=HEADERS, json=body)
    assert replay.status_code == 201
    assert replay.json()["id"] == token
    conflict = client.post(
        "/agentic_commerce/delegate_payment",
        headers=HEADERS,
        json={**body, "ap2_checkout_mandate": hnp.close_checkout()},
    )
    assert conflict.status_code == 409
    assert conflict.json() == {"code": "idempotency_conflict"}

    confirmed = client.post(
        "/payments/confirm",
        headers={"Authorization": f"Bearer {MERCHANT_SECRET}", "Idempotency-Key": "op_1:confirm"},
        json={
            "token": token,
            "merchant_id": "tesis_merchant",
            "checkout_session_id": "chk_test_1",
            "amount": TOTAL,
            "currency": "cop",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    provider_jwk = client.get("/.well-known/ap2/jwks.json").json()["keys"][0]
    claims = jwt.decode(
        confirmed.json()["payment_receipt_jwt"],
        jwt.PyJWK.from_dict(provider_jwk).key,
        algorithms=["ES256"],
        issuer="tesis_payment_provider",
    )
    receipt = PaymentReceiptSuccess.model_validate(claims)
    last = body["ap2_payment_mandate"].rsplit("~~", 1)[1]
    assert receipt.reference == common.compute_sd_hash(common.parse_token(last))
    database = (tmp_path / "payments.db").read_bytes()
    for secret in (body["ap2_payment_mandate"], body["ap2_checkout_mandate"], token):
        assert secret.split("~")[0].encode() not in database


@pytest.mark.parametrize("alteration", ALTERATIONS)
def test_altered_chain_is_rejected_with_a_signed_ap2_error_receipt(
    alteration: str, hnp: Hnp, client: TestClient, tmp_path: Path
) -> None:
    build, code = ALTERATIONS[alteration]
    body = build(hnp)

    rejected = client.post("/agentic_commerce/delegate_payment", headers=HEADERS, json=body)

    assert rejected.status_code == 422, rejected.text
    assert set(rejected.json()) == {"code", "payment_receipt_jwt"}
    assert rejected.json()["code"] == code
    provider_jwk = client.get("/.well-known/ap2/jwks.json").json()["keys"][0]
    receipt = PaymentReceiptError.model_validate(
        jwt.decode(
            rejected.json()["payment_receipt_jwt"],
            jwt.PyJWK.from_dict(provider_jwk).key,
            algorithms=["ES256"],
            issuer="tesis_payment_provider",
        )
    )
    last = body["ap2_payment_mandate"].rsplit("~~", 1)[-1]
    assert receipt.error == code
    assert receipt.reference == common.compute_sd_hash(common.parse_token(last))
    assert receipt.payment_id == "pay_rejected_" + hashlib.sha256(b"op_1:payment").hexdigest()[:32]
    assert _rows(tmp_path / "payments.db") == 0


@pytest.mark.parametrize("position", ["root", "closed"])
def test_unexpected_typ_for_the_position_is_invalid_credential(
    position: str, hnp: Hnp, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    if position == "root":
        header_parameters = common.header_parameters
        monkeypatch.setattr(
            common,
            "header_parameters",
            lambda key, typ=None: header_parameters(key, typ or "kb+sd-jwt"),
        )
    else:  # the SDK also accepts the `kb-sd-jwt` spelling; the profile fixes `kb+sd-jwt`
        monkeypatch.setattr(kb_sd_jwt, "TYP_TERMINAL", ["kb-sd-jwt", "kb+sd-jwt"])
    body = hnp.body()
    monkeypatch.undo()

    rejected = client.post("/agentic_commerce/delegate_payment", headers=HEADERS, json=body)

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "invalid_credential"


def test_unparseable_last_segment_receipt_hashes_the_segment_as_received(
    hnp: Hnp, client: TestClient
) -> None:
    chain = hnp.close_payment().split("~~")[0] + "~~not-a-token"
    rejected = client.post(
        "/agentic_commerce/delegate_payment",
        headers=HEADERS,
        json=hnp.body(ap2_payment_mandate=chain),
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "invalid_credential"
    claims = jwt.decode(rejected.json()["payment_receipt_jwt"], options={"verify_signature": False})
    assert claims["reference"] == _sha256("not-a-token")


def test_rejected_retry_repeats_the_receipt_payment_id(hnp: Hnp, client: TestClient) -> None:
    body = hnp.body(ap2_payment_mandate=hnp.close_payment(payee={"id": "x", "name": "X"}))
    ids = set()
    for _ in range(2):
        rejected = client.post("/agentic_commerce/delegate_payment", headers=HEADERS, json=body)
        assert rejected.status_code == 422
        claims = jwt.decode(
            rejected.json()["payment_receipt_jwt"], options={"verify_signature": False}
        )
        ids.add(claims["payment_id"])
    assert len(ids) == 1


def test_same_body_and_key_returns_the_token_after_the_closed_mandate_expires(
    hnp: Hnp, tmp_path: Path
) -> None:
    current = [datetime.fromtimestamp(hnp.now, UTC)]
    store = PaymentStore(
        tmp_path / "payments.db",
        SecretStr("token-secret-with-at-least-thirty-two-characters"),
        "tesis_merchant",
        clock=lambda: datetime.fromtimestamp(hnp.now, UTC),
        ap2=hnp.ap2(clock=lambda: current[0]),
    )
    store.initialize()
    request = DelegatePaymentRequest.model_validate(hnp.body())
    token = store.delegate(request, idempotency_key="op_1:payment").id

    current[0] += timedelta(minutes=10)
    assert store.delegate(request, idempotency_key="op_1:payment").id == token
    with pytest.raises(PaymentError) as expired:
        store.delegate(request, idempotency_key="op_2:payment")
    assert expired.value.code == "invalid_credential"
    assert expired.value.status_code == 422
    assert expired.value.receipt is not None


def test_provider_without_ap2_answers_chains_with_503_and_no_receipt(
    hnp: Hnp, tmp_path: Path
) -> None:
    now = datetime.fromtimestamp(hnp.now, UTC)
    client = TestClient(
        create_app(hnp.settings(tmp_path / "payments.db", ap2=False), clock=lambda: now)
    )
    rejected = client.post("/agentic_commerce/delegate_payment", headers=HEADERS, json=hnp.body())
    assert rejected.status_code == 503
    assert rejected.json() == {"code": "mandates_not_supported"}


def test_checkout_chain_enters_the_fingerprint_only_when_present() -> None:
    """Human Present bodies dump exactly as before, so their idempotent tokens do not change."""
    human_present = DelegatePaymentRequest.model_validate(delegation())
    human_not_present = DelegatePaymentRequest.model_validate(
        {**delegation(), "ap2_checkout_mandate": "open~~closed~"}
    )
    assert "ap2_checkout_mandate" not in human_present.model_dump(mode="json")
    assert human_not_present.model_dump(mode="json")["ap2_checkout_mandate"] == "open~~closed~"
