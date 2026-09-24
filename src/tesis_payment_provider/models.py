from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TestCard(StrictModel):
    type: Literal["card"]
    card_number_type: Literal["fpan"]
    number: str = Field(pattern=r"^[0-9]{16}$")
    exp_month: str = Field(pattern=r"^(0[1-9]|1[0-2])$")
    exp_year: str = Field(pattern=r"^[0-9]{4}$")
    display_card_funding_type: Literal["credit"]
    metadata: dict[str, str]


class Allowance(StrictModel):
    reason: Literal["one_time"]
    max_amount: int = Field(gt=0)
    currency: str = Field(pattern=r"^[a-z]{3}$")
    checkout_session_id: str = Field(min_length=1, max_length=128)
    merchant_id: str = Field(min_length=1, max_length=128)
    expires_at: datetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def require_timezone(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            raise ValueError("Allowance expiry needs a timezone")
        return value


class RiskSignal(StrictModel):
    type: Literal["card_testing"]
    score: int = Field(ge=0, le=100)
    action: Literal["authorized"]


class DelegatePaymentRequest(StrictModel):
    payment_method: TestCard
    allowance: Allowance
    risk_signals: list[RiskSignal] = Field(min_length=1)
    metadata: dict[str, str]
    ap2_payment_mandate: str | None = Field(default=None, max_length=32_768, repr=False)
    ap2_checkout_jwt: str | None = Field(default=None, max_length=32_768, repr=False)


class DelegatePaymentResponse(StrictModel):
    id: str
    created: datetime
    metadata: dict[str, str]


class ConfirmPaymentRequest(StrictModel):
    token: SecretStr
    merchant_id: str
    checkout_session_id: str
    amount: int = Field(gt=0)
    currency: str = Field(pattern=r"^[a-z]{3}$")


class PaymentResult(StrictModel):
    id: str
    status: Literal["approved", "declined"]
    merchant_id: str
    checkout_session_id: str
    amount: int
    currency: str
    payment_receipt_jwt: str | None = Field(default=None, exclude_if=lambda value: value is None)


class LookupPaymentRequest(StrictModel):
    token: SecretStr


class PaymentLookup(StrictModel):
    status: Literal["not_confirmed", "approved", "declined"]
    result: PaymentResult | None = None
