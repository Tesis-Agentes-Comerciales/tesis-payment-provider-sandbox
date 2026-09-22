import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from tesis_payment_provider.models import (
    ConfirmPaymentRequest,
    DelegatePaymentRequest,
    DelegatePaymentResponse,
    PaymentLookup,
    PaymentResult,
)

TEST_CARDS = {
    "4242424242424242": "approved",
    "4000000000000002": "declined",
}


class PaymentError(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fingerprint(value: object) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


class PaymentStore:
    def __init__(
        self,
        path: Path,
        token_secret: SecretStr,
        merchant_id: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = path
        self._secret = token_secret
        self._merchant_id = merchant_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS delegations (
                    key_hash TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    merchant_id TEXT NOT NULL,
                    checkout_id TEXT NOT NULL,
                    max_amount INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    confirmation_key_hash TEXT,
                    confirmation_hash TEXT,
                    result_json TEXT
                );
                """
            )

    def delegate(
        self, request: DelegatePaymentRequest, *, idempotency_key: str
    ) -> DelegatePaymentResponse:
        key_hash = _digest(idempotency_key)
        request_hash = _fingerprint(request.model_dump(mode="json"))
        token = self._token(key_hash, request_hash)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_hash, created_at FROM delegations WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise PaymentError("idempotency_conflict", 409)
                created = datetime.fromisoformat(existing["created_at"])
                return DelegatePaymentResponse(id=token, created=created, metadata={})

            now = self._clock()
            if request.allowance.merchant_id != self._merchant_id:
                raise PaymentError("merchant_mismatch", 422)
            if request.allowance.expires_at <= now:
                raise PaymentError("allowance_expired", 422)
            outcome = TEST_CARDS.get(request.payment_method.number)
            if outcome is None:
                raise PaymentError("invalid_test_card", 422)
            if int(request.payment_method.exp_year) < now.year:
                raise PaymentError("expired_test_card", 422)
            connection.execute(
                """INSERT INTO delegations
                   (key_hash, request_hash, token_hash, created_at, merchant_id,
                    checkout_id, max_amount, currency, expires_at, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key_hash,
                    request_hash,
                    _digest(token),
                    now.isoformat(),
                    request.allowance.merchant_id,
                    request.allowance.checkout_session_id,
                    request.allowance.max_amount,
                    request.allowance.currency,
                    request.allowance.expires_at.isoformat(),
                    outcome,
                ),
            )
            return DelegatePaymentResponse(id=token, created=now, metadata={})

    def confirm(self, request: ConfirmPaymentRequest, *, idempotency_key: str) -> PaymentResult:
        token = request.token.get_secret_value()
        key_hash = _digest(idempotency_key)
        request_hash = _fingerprint(
            request.model_dump(mode="json", exclude={"token"}) | {"token_hash": _digest(token)}
        )
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM delegations WHERE token_hash = ?", (_digest(token),)
            ).fetchone()
            if row is None:
                raise PaymentError("unknown_token", 422)
            if row["result_json"] is not None:
                if (
                    row["confirmation_key_hash"] != key_hash
                    or row["confirmation_hash"] != request_hash
                ):
                    raise PaymentError("token_already_used", 409)
                return PaymentResult.model_validate_json(row["result_json"])
            if datetime.fromisoformat(row["expires_at"]) <= self._clock():
                raise PaymentError("allowance_expired", 422)
            if (
                request.merchant_id != row["merchant_id"]
                or request.checkout_session_id != row["checkout_id"]
                or request.currency != row["currency"]
                or request.amount > row["max_amount"]
            ):
                raise PaymentError("allowance_mismatch", 422)
            result = PaymentResult(
                id=f"pay_{row['token_hash'][:32]}",
                status=row["outcome"],
                merchant_id=request.merchant_id,
                checkout_session_id=request.checkout_session_id,
                amount=request.amount,
                currency=request.currency,
            )
            connection.execute(
                """UPDATE delegations
                   SET confirmation_key_hash = ?, confirmation_hash = ?, result_json = ?
                   WHERE token_hash = ?""",
                (key_hash, request_hash, result.model_dump_json(), row["token_hash"]),
            )
            return result

    def lookup(self, token: SecretStr) -> PaymentLookup:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT result_json FROM delegations WHERE token_hash = ?",
                (_digest(token.get_secret_value()),),
            ).fetchone()
        if row is None:
            raise PaymentError("unknown_token", 422)
        if row["result_json"] is None:
            return PaymentLookup(status="not_confirmed")
        result = PaymentResult.model_validate_json(row["result_json"])
        return PaymentLookup(status=result.status, result=result)

    def _token(self, key_hash: str, request_hash: str) -> str:
        digest = hmac.new(
            self._secret.get_secret_value().encode(),
            f"{key_hash}:{request_hash}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"vt_{digest}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection
