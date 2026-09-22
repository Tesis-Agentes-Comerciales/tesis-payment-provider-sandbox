import hmac
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import SecretStr

from tesis_payment_provider.config import Settings
from tesis_payment_provider.models import (
    ConfirmPaymentRequest,
    DelegatePaymentRequest,
    DelegatePaymentResponse,
    LookupPaymentRequest,
    PaymentLookup,
    PaymentResult,
)
from tesis_payment_provider.store import PaymentError, PaymentStore

VERSION = "2026-04-17"


def create_app(
    settings: Settings | None = None, *, clock: Callable[[], datetime] | None = None
) -> FastAPI:
    config = settings or Settings()
    config.validate_secrets()
    sandbox_clock = config.payment_sandbox_clock_at
    if clock is None and sandbox_clock is not None:

        def fixed_sandbox_clock() -> datetime:
            return sandbox_clock

        clock = fixed_sandbox_clock
    store = PaymentStore(
        config.payment_database_path,
        config.payment_token_secret,
        config.payment_merchant_id,
        clock=clock,
    )
    store.initialize()
    app = FastAPI(title="Tesis Payment Provider Sandbox", version="0.1.0")

    def authorize(expected: SecretStr, authorization: str | None) -> None:
        prefix = "Bearer "
        if (
            authorization is None
            or not authorization.startswith(prefix)
            or not hmac.compare_digest(authorization[len(prefix) :], expected.get_secret_value())
        ):
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})

    def agent_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        authorize(config.payment_agent_bearer_token, authorization)

    def merchant_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        authorize(config.payment_merchant_bearer_token, authorization)

    def required_version(api_version: Annotated[str | None, Header()] = None) -> None:
        if api_version != VERSION:
            raise HTTPException(status_code=400, detail={"code": "unsupported_api_version"})

    def required_key(idempotency_key: Annotated[str | None, Header()] = None) -> str:
        if idempotency_key is None or not 1 <= len(idempotency_key) <= 255:
            raise HTTPException(status_code=400, detail={"code": "invalid_idempotency_key"})
        return idempotency_key

    @app.exception_handler(PaymentError)
    def payment_error_handler(_, error: PaymentError):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=error.status_code, content={"code": error.code})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/agentic_commerce/delegate_payment",
        status_code=201,
        dependencies=[Depends(agent_auth), Depends(required_version)],
    )
    def delegate_payment(
        request: DelegatePaymentRequest,
        key: Annotated[str, Depends(required_key)],
    ) -> DelegatePaymentResponse:
        return store.delegate(request, idempotency_key=key)

    @app.post("/payments/confirm", dependencies=[Depends(merchant_auth)])
    def confirm_payment(
        request: ConfirmPaymentRequest,
        key: Annotated[str, Depends(required_key)],
    ) -> PaymentResult:
        return store.confirm(request, idempotency_key=key)

    @app.post("/payments/lookup", dependencies=[Depends(merchant_auth)])
    def lookup_payment(request: LookupPaymentRequest) -> PaymentLookup:
        return store.lookup(request.token)

    return app


def app_factory() -> FastAPI:
    return create_app()
