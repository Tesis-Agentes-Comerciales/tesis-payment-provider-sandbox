from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tesis_payment_provider.api import create_app
from tesis_payment_provider.config import Settings

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=UTC)
AGENT_SECRET = "agent-secret-with-at-least-thirty-two-characters"
MERCHANT_SECRET = "merchant-secret-with-at-least-thirty-two-characters"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        payment_database_path=tmp_path / "payments.db",
        payment_agent_bearer_token=SecretStr(AGENT_SECRET),
        payment_merchant_bearer_token=SecretStr(MERCHANT_SECRET),
        payment_token_secret=SecretStr("token-secret-with-at-least-thirty-two-characters"),
    )
    return TestClient(create_app(settings, clock=lambda: NOW))


def delegation(card: str = "4242424242424242") -> dict[str, object]:
    return {
        "payment_method": {
            "type": "card",
            "card_number_type": "fpan",
            "number": card,
            "exp_month": "12",
            "exp_year": "2035",
            "display_card_funding_type": "credit",
            "metadata": {},
        },
        "allowance": {
            "reason": "one_time",
            "max_amount": 62000,
            "currency": "usd",
            "checkout_session_id": "chk_test_1",
            "merchant_id": "tesis_merchant",
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        },
        "risk_signals": [{"type": "card_testing", "score": 0, "action": "authorized"}],
        "metadata": {},
    }


def delegate(client: TestClient, *, card: str = "4242424242424242", key: str = "delegate-1"):
    return client.post(
        "/agentic_commerce/delegate_payment",
        headers={
            "Authorization": f"Bearer {AGENT_SECRET}",
            "API-Version": "2026-04-17",
            "Idempotency-Key": key,
        },
        json=delegation(card),
    )


def confirm(client: TestClient, token: str, *, key: str = "confirm-1", amount: int = 62000):
    return client.post(
        "/payments/confirm",
        headers={
            "Authorization": f"Bearer {MERCHANT_SECRET}",
            "Idempotency-Key": key,
        },
        json={
            "token": token,
            "merchant_id": "tesis_merchant",
            "checkout_session_id": "chk_test_1",
            "amount": amount,
            "currency": "usd",
        },
    )


def test_token_is_opaque_scoped_single_use_and_idempotent(client: TestClient, tmp_path: Path):
    first = delegate(client)
    replay = delegate(client)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    token = first.json()["id"]
    assert token.startswith("vt_")
    assert "success" not in token

    wrong_amount = confirm(client, token, amount=62001)
    assert wrong_amount.status_code == 422
    assert wrong_amount.json()["code"] == "allowance_mismatch"

    paid = confirm(client, token)
    assert paid.status_code == 200
    assert paid.json()["status"] == "approved"
    assert confirm(client, token).json() == paid.json()
    assert confirm(client, token, key="different-key").status_code == 409
    assert token.encode() not in (tmp_path / "payments.db").read_bytes()
    assert b"4242424242424242" not in (tmp_path / "payments.db").read_bytes()


def test_decline_and_conflicting_delegation(client: TestClient):
    first = delegate(client, card="4000000000000002")
    assert first.status_code == 201
    assert delegate(client).status_code == 409
    result = confirm(client, first.json()["id"])
    assert result.status_code == 200
    assert result.json()["status"] == "declined"


def test_wrong_actor_cannot_issue_or_confirm(client: TestClient):
    issued = client.post(
        "/agentic_commerce/delegate_payment",
        headers={
            "Authorization": f"Bearer {MERCHANT_SECRET}",
            "API-Version": "2026-04-17",
            "Idempotency-Key": "wrong-actor",
        },
        json=delegation(),
    )
    assert issued.status_code == 401
    valid = delegate(client)
    confirmed = client.post(
        "/payments/confirm",
        headers={
            "Authorization": f"Bearer {AGENT_SECRET}",
            "Idempotency-Key": "wrong-actor",
        },
        json={
            "token": valid.json()["id"],
            "merchant_id": "tesis_merchant",
            "checkout_session_id": "chk_test_1",
            "amount": 62000,
            "currency": "usd",
        },
    )
    assert confirmed.status_code == 401


def test_allowance_rejects_other_checkout_merchant_and_currency(client: TestClient) -> None:
    issued = delegate(client)
    token = issued.json()["id"]
    for changes in (
        {"checkout_session_id": "another-checkout"},
        {"merchant_id": "another-merchant"},
        {"currency": "cop"},
    ):
        body = {
            "token": token,
            "merchant_id": "tesis_merchant",
            "checkout_session_id": "chk_test_1",
            "amount": 62000,
            "currency": "usd",
        } | changes
        response = client.post(
            "/payments/confirm",
            headers={
                "Authorization": f"Bearer {MERCHANT_SECRET}",
                "Idempotency-Key": "confirm-scope",
            },
            json=body,
        )
        assert response.status_code == 422
        assert response.json()["code"] == "allowance_mismatch"
    assert confirm(client, token).json()["status"] == "approved"


def test_expired_allowance_and_unknown_token_fail_closed(client: TestClient) -> None:
    expired = delegation()
    allowance = expired["allowance"]
    assert isinstance(allowance, dict)
    allowance["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    response = client.post(
        "/agentic_commerce/delegate_payment",
        headers={
            "Authorization": f"Bearer {AGENT_SECRET}",
            "API-Version": "2026-04-17",
            "Idempotency-Key": "expired-delegate",
        },
        json=expired,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "allowance_expired"
    assert confirm(client, "vt_" + "f" * 64).status_code == 422
