import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import jwt
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_payment_mandate import (
    AllowedPayees,
    AllowedPaymentInstruments,
    AmountRange,
    PaymentReference,
)
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.mandate import MandateClient
from ap2.sdk.payment_mandate_chain import PaymentMandateChain
from ap2.sdk.sdjwt import common, sd_jwt
from jwcrypto.common import JWException
from jwcrypto.jwk import JWK
from jwt.utils import base64url_encode
from pydantic import BaseModel

from tesis_payment_provider.models import DelegatePaymentRequest

RECEIPT_ISSUER = "tesis_payment_provider"
CLOCK_SKEW_SECONDS = 60
MAX_CLOSED_LIFETIME_SECONDS = 300
_ROOT_TYP = "example+sd-jwt"
_CLOSED_TYP = "kb+sd-jwt"
_PAYMENT_VCT = ("mandate.payment.open.1", "mandate.payment.1")
_CHECKOUT_VCT = ("mandate.checkout.open.1", "mandate.checkout.1")
_PRESET_PAYMENT_CLAIMS = ("payee", "payment_amount", "payment_instrument", "pisp", "execution_date")


def _fields(model: type[BaseModel]) -> frozenset[str]:
    return frozenset(field.alias or name for name, field in model.model_fields.items())


# Profile `ap2-hnp/v1`: the only payment constraints the PSP evaluates, with the
# properties the SDK model knows (the SDK silently drops any other property).
_PAYMENT_CONSTRAINTS: dict[str, tuple[frozenset[str], frozenset[str] | None]] = {
    "payment.amount_range": (_fields(AmountRange), None),
    "payment.allowed_payees": (_fields(AllowedPayees), _fields(Merchant)),
    "payment.allowed_payment_instruments": (
        _fields(AllowedPaymentInstruments),
        _fields(PaymentInstrument),
    ),
    "payment.reference": (_fields(PaymentReference), None),
}


class PaymentMandateError(ValueError):
    pass


class MandateChainRejected(ValueError):
    """A Human Not Present chain failed verification; the PSP answers with an error receipt."""

    def __init__(self, code: str, description: str, reference: str) -> None:
        super().__init__(code)
        self.code = code
        self.description = description
        self.reference = reference


def is_mandate_chain(request: DelegatePaymentRequest) -> bool:
    """Human Not Present requests carry a `~~` chain or the checkout mandate chain."""
    return request.ap2_checkout_mandate is not None or "~~" in (request.ap2_payment_mandate or "")


def chain_reference(chain: str) -> str:
    """Receipt `reference`: `sd_hash` of the last segment, or the hash of it as received."""
    last = chain.rsplit("~~", 1)[-1]
    try:
        return common.compute_sd_hash(common.parse_token(last))
    except ValueError:
        return _sha256(last)


def _sha256(value: str) -> str:
    return base64url_encode(hashlib.sha256(value.encode()).digest()).decode()


def _in_time(payload: dict[str, Any], now: int) -> bool:
    """`iat` and `exp` present; `iat` may run 60 s ahead, `exp` is strict (profile)."""
    iat, exp = payload.get("iat"), payload.get("exp")
    return (
        isinstance(iat, int)
        and isinstance(exp, int)
        and not isinstance(iat, bool)
        and not isinstance(exp, bool)
        and iat <= now + CLOCK_SKEW_SECONDS
        and now < exp
    )


def _open_payment_in_profile(payload: dict[str, Any]) -> bool:
    if any(claim in payload for claim in _PRESET_PAYMENT_CLAIMS):
        return False
    constraints = payload.get("constraints")
    if not isinstance(constraints, list):
        return False
    seen: list[str] = []
    for constraint in constraints:
        kind = constraint.get("type") if isinstance(constraint, dict) else None
        if not isinstance(kind, str) or kind not in _PAYMENT_CONSTRAINTS:
            return False
        properties, item_properties = _PAYMENT_CONSTRAINTS[kind]
        if not constraint.keys() <= properties:
            return False
        if item_properties is not None:
            allowed = constraint.get("allowed")
            if not isinstance(allowed, list) or any(
                not isinstance(item, dict) or not item.keys() <= item_properties for item in allowed
            ):
                return False
        seen.append(kind)
    return sorted(seen) == sorted(_PAYMENT_CONSTRAINTS)


class _PinnedRootKey:
    """Resolve a chain root only to the pinned agent-provider key: `kid` = RFC 7638
    thumbprint and no `x5c` (the SDK's `X5cOrKidPublicKeyProvider` would accept any x5c)."""

    def __init__(self, key: JWK) -> None:
        self._key = key
        self._kid = key.thumbprint()

    def __call__(self, token: common.ParsedToken) -> JWK:
        if "x5c" in token.header or token.header.get("kid") != self._kid:
            raise ValueError("Root mandate is not signed by the pinned agent provider key")
        return self._key


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
        self._root_key = _PinnedRootKey(self._agent_key)
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
            checkout = self._checkout(checkout_jwt)
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
                or not _closed_matches(mandate, request, merchant_id)
                or not _checkout_matches(checkout, request)
            ):
                raise ValueError("Payment mandate does not match checkout")
            return common.compute_sd_hash(common.parse_token(token))
        except (KeyError, TypeError, ValueError, jwt.PyJWTError, JWException) as error:
            raise PaymentMandateError("Invalid AP2 payment authorization") from error

    def verify_chain(self, request: DelegatePaymentRequest, merchant_id: str) -> str:
        """Verify a Human Not Present payment chain (profile `ap2-hnp/v1`, PSP section).

        Returns the receipt `reference` of the closed payment mandate. Raises
        `MandateChainRejected` with the AP2 error code otherwise.
        """
        payment_chain = request.ap2_payment_mandate or ""
        reference = chain_reference(payment_chain)

        def reject(code: str, description: str) -> MandateChainRejected:
            return MandateChainRejected(code, description, reference)

        now = int(self._clock().timestamp())
        checkout_jwt = request.ap2_checkout_jwt
        if checkout_jwt is None:
            raise reject("invalid_credential", "The checkout JWT is required")
        transaction_id = _sha256(checkout_jwt)
        payment = self._verified_chain(
            payment_chain, RECEIPT_ISSUER, transaction_id, _PAYMENT_VCT, now
        )
        if payment is None:
            raise reject("invalid_credential", "The payment mandate chain does not verify")
        if request.ap2_checkout_mandate is None:
            raise reject("unresolved_constraint", "The checkout mandate chain is required")
        checkout = self._verified_chain(
            request.ap2_checkout_mandate,
            self._merchant_issuer,
            transaction_id,
            _CHECKOUT_VCT,
            now,
        )
        if checkout is None:
            raise reject("invalid_credential", "The checkout mandate chain does not verify")
        if not _open_payment_in_profile(payment[0]):
            raise reject("unresolved_constraint", "The open payment mandate is outside the profile")
        try:
            chain = PaymentMandateChain.parse(payment)
            closed_checkout = CheckoutMandate.model_validate(checkout[1])
            open_checkout_hash = common.compute_issuer_jwt_hash(
                common.parse_token(request.ap2_checkout_mandate.split("~", 1)[0] + "~")
            )
        except ValueError:
            raise reject("invalid_credential", "A mandate payload is malformed") from None
        if chain.verify(
            expected_transaction_id=transaction_id,
            expected_open_checkout_hash=open_checkout_hash,
        ):
            raise reject("invalid_mandate", "The closed payment violates its open mandate")
        closed = chain.closed_mandate
        if (
            closed_checkout.checkout_hash != closed.transaction_id
            or closed_checkout.checkout_jwt != checkout_jwt
        ):
            raise reject("invalid_mandate", "The checkout mandate is bound to another checkout")
        if any(
            not 0 < hop[1]["exp"] - hop[1]["iat"] <= MAX_CLOSED_LIFETIME_SECONDS
            for hop in (payment, checkout)
        ):
            raise reject("invalid_mandate", "A closed mandate outlives the profile")
        try:
            checkout_document = self._checkout(checkout_jwt)
        except (KeyError, TypeError, ValueError, jwt.PyJWTError):
            raise reject("invalid_credential", "The checkout JWT does not verify") from None
        try:
            matches = _closed_matches(closed, request, merchant_id) and _checkout_matches(
                checkout_document, request
            )
        except (KeyError, TypeError, AttributeError):
            matches = False
        if not matches:
            raise reject("invalid_mandate", "The payment mandate does not match the checkout")
        return reference

    def _verified_chain(
        self, chain: str, audience: str, nonce: str, vct: tuple[str, str], now: int
    ) -> list[dict[str, Any]] | None:
        """Cryptographic and structural checks; `None` means `invalid_credential`."""
        segments = chain.split("~~")
        try:
            if len(segments) != 2:
                return None
            root = common.parse_token(segments[0].split("~", 1)[0] + "~")
            closed = common.parse_token(segments[1])
            payloads = MandateClient().verify(
                chain,
                self._root_key,
                expected_aud=audience,
                expected_nonce=nonce,
                clock_skew_seconds=CLOCK_SKEW_SECONDS,
                current_time=now,
            )
        except Exception:
            # Fail closed: the SDK and its crypto libraries raise many exception types.
            return None
        if (
            not isinstance(payloads, list)
            or len(payloads) != 2
            or root.typ != _ROOT_TYP
            or closed.typ != _CLOSED_TYP
            or "sd_hash" not in closed.payload
        ):
            return None
        if any(
            payload.get("vct") != expected_vct or not _in_time(payload, now)
            for payload, expected_vct in zip(payloads, vct, strict=True)
        ):
            return None
        return payloads

    def _checkout(self, checkout_jwt: str) -> dict[str, Any]:
        claims = jwt.decode(
            checkout_jwt,
            self._merchant_key,
            algorithms=["ES256"],
            audience="ap2-shopping-agent",
            issuer=self._merchant_issuer,
            options={"require": ["iss", "aud", "iat", "exp", "jti", "checkout"]},
        )
        return claims["checkout"]

    def receipt(self, *, reference: str, payment_id: str, approved: bool) -> str:
        now = int(self._clock().timestamp())
        claims = {
            "status": "Success" if approved else "Error",
            "iss": RECEIPT_ISSUER,
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
        return self._sign(claims)

    def error_receipt(self, rejection: MandateChainRejected, *, payment_id: str) -> str:
        """`PaymentReceiptError` for a rejected Human Not Present chain."""
        return self._sign(
            {
                "status": "Error",
                "iss": RECEIPT_ISSUER,
                "iat": int(self._clock().timestamp()),
                "reference": rejection.reference,
                "error": rejection.code,
                "error_description": rejection.description,
                "payment_id": payment_id,
            }
        )

    def _sign(self, claims: dict[str, object]) -> str:
        return jwt.encode(claims, self._receipt_key, algorithm="ES256", headers={"typ": "JWT"})


def _closed_matches(
    mandate: PaymentMandate, request: DelegatePaymentRequest, merchant_id: str
) -> bool:
    return (
        mandate.payee.id == merchant_id
        and mandate.payment_instrument.id == "tesis_sandbox"
        and mandate.payment_amount.amount == request.allowance.max_amount
        and mandate.payment_amount.currency.lower() == request.allowance.currency
    )


def _checkout_matches(checkout: dict[str, Any], request: DelegatePaymentRequest) -> bool:
    totals = [
        item["amount"]
        for item in checkout["totals"]
        if isinstance(item, dict) and item.get("type") == "total"
    ]
    return (
        checkout["id"] == request.allowance.checkout_session_id
        and checkout["status"] == "ready_for_payment"
        and checkout["currency"] == request.allowance.currency
        and len(totals) == 1
        and totals[0] == request.allowance.max_amount
    )
