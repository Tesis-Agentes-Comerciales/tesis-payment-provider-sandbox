from datetime import datetime
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    payment_database_path: Path = Path(".local/payments.db")
    payment_agent_bearer_token: SecretStr = Field(default=SecretStr(""), repr=False)
    payment_merchant_bearer_token: SecretStr = Field(default=SecretStr(""), repr=False)
    payment_token_secret: SecretStr = Field(default=SecretStr(""), repr=False)
    payment_merchant_id: str = Field(default="tesis_merchant", min_length=1)
    payment_sandbox_clock_at: datetime | None = None
    payment_ap2_required: bool = False
    payment_ap2_agent_jwk_json: SecretStr | None = Field(default=None, repr=False)
    payment_ap2_merchant_jwk_json: SecretStr | None = Field(default=None, repr=False)
    payment_ap2_merchant_issuer: str | None = None
    payment_ap2_private_key_pem: SecretStr | None = Field(default=None, repr=False)

    @field_validator("payment_sandbox_clock_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Sandbox clock must include a timezone")
        return value

    def validate_secrets(self) -> None:
        for secret in (
            self.payment_agent_bearer_token,
            self.payment_merchant_bearer_token,
            self.payment_token_secret,
        ):
            if len(secret.get_secret_value()) < 32:
                raise ValueError("Payment secrets must contain at least 32 characters")
        if self.payment_ap2_required and not all(
            (
                self.payment_ap2_agent_jwk_json,
                self.payment_ap2_merchant_jwk_json,
                self.payment_ap2_merchant_issuer,
                self.payment_ap2_private_key_pem,
            )
        ):
            raise ValueError("AP2 payment verification and receipt signing must be configured")
