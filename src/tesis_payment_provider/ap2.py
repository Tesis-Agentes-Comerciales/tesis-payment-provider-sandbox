import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

import jwt
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.sdjwt import common, sd_jwt
from jwcrypto.common import JWException
from jwcrypto.jwk import JWK
from jwt.utils import base64url_encode

from tesis_payment_provider.models import DelegatePaymentRequest


class PaymentMandateError(ValueError):
    pass


class PaymentAp2:
    def __init__(
        self,
        *,
        agent_jwk_json: str,
        merchant_jwk_json: str,
        merchant_issuer: str,
        private_key_pem: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        agent_jwk = json.loads(agent_jwk_json)
        merchant_jwk = json.loads(merchant_jwk_json)
        if any(
            not isinstance(key, dict)
            or key.get("kty") != "EC"
            or key.get("crv") != "P-256"
            or "d" in key
            for key in (agent_jwk, merchant_jwk)
        ):
            raise ValueError("AP2 trust keys must be public P-256 JWKs")
        self._agent_key = JWK.from_json(agent_jwk_json)
        self._merchant_key = jwt.PyJWK.from_dict(merchant_jwk).key
        self._merchant_issuer = merchant_issuer
        self._receipt_key = private_key_pem.replace("\\n", "\n")
        receipt_key = JWK.from_pem(self._receipt_key.encode())
        if (
            receipt_key.get("kty") != "EC"
            or receipt_key.get("crv") != "P-256"
            or not receipt_key.get("d")
        ):
            raise ValueError("AP2 receipt signing requires an ES256 private key")
        self._receipt_public_jwk = json.loads(receipt_key.export_public())
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def public_jwk(self) -> dict[str, object]:
        return self._receipt_public_jwk

    def verify(self, request: DelegatePaymentRequest, merchant_id: str) -> str:
        token = request.ap2_payment_mandate
        checkout_jwt = request.ap2_checkout_jwt
        if token is None or checkout_jwt is None:
            raise PaymentMandateError("AP2 mandates are required")
        try:
            verified = sd_jwt.verify(token, self._agent_key)
            contents = verified["delegate_payload"]
            if not isinstance(contents, list) or len(contents) != 1:
                raise ValueError("Invalid payment mandate payload")
            mandate = PaymentMandate.model_validate(contents[0])
            checkout_claims = jwt.decode(
                checkout_jwt,
                self._merchant_key,
                algorithms=["ES256"],
                audience="ap2-shopping-agent",
                issuer=self._merchant_issuer,
                options={"require": ["iss", "aud", "iat", "exp", "jti", "checkout"]},
            )
            checkout = checkout_claims["checkout"]
            totals = [
                item["amount"]
                for item in checkout["totals"]
                if isinstance(item, dict) and item.get("type") == "total"
            ]
            now = int(self._clock().timestamp())
            digest = base64url_encode(
                hashlib.sha256(checkout_jwt.encode("ascii")).digest()
            ).decode()
            if (
                mandate.iat is None
                or mandate.exp is None
                or mandate.iat > now + 60
                or mandate.exp <= mandate.iat
                or mandate.exp - mandate.iat > 300
                or mandate.exp <= now
                or mandate.transaction_id != digest
                or mandate.payee.id != merchant_id
                or mandate.payment_instrument.id != "tesis_sandbox"
                or mandate.payment_amount.amount != request.allowance.max_amount
                or mandate.payment_amount.currency.lower() != request.allowance.currency
                or checkout["id"] != request.allowance.checkout_session_id
                or checkout["status"] != "ready_for_payment"
                or checkout["currency"] != request.allowance.currency
                or len(totals) != 1
                or totals[0] != request.allowance.max_amount
            ):
                raise ValueError("Payment mandate does not match checkout")
            return common.compute_sd_hash(common.parse_token(token))
        except (KeyError, TypeError, ValueError, jwt.PyJWTError, JWException) as error:
            raise PaymentMandateError("Invalid AP2 payment authorization") from error

    def receipt(self, *, reference: str, payment_id: str, approved: bool) -> str:
        now = int(self._clock().timestamp())
        claims = {
            "status": "Success" if approved else "Error",
            "iss": "tesis_payment_provider",
            "iat": now,
            "reference": reference,
            "payment_id": payment_id,
        }
        if approved:
            claims["psp_confirmation_id"] = payment_id
            claims["network_confirmation_id"] = f"sandbox_{payment_id}"
        else:
            claims["error"] = "payment_declined"
            claims["error_description"] = "Synthetic payment was declined"
        return jwt.encode(claims, self._receipt_key, algorithm="ES256", headers={"typ": "JWT"})
