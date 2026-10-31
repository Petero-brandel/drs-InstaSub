"""
DRSub – Paystack API client.

Handles Dedicated Virtual Account (DVA) creation and payment verification
for accepting bank transfers in Naira.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.paystack.co"
_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


# ── Data classes ─────────────────────────────────────────────

@dataclass(frozen=True)
class DVAInfo:
    """Details of a Dedicated Virtual Account."""
    account_number: str
    bank_name: str
    account_name: str
    customer_code: str


@dataclass(frozen=True)
class PaymentEvent:
    """Parsed payment event from a Paystack webhook."""
    reference: str
    amount_kobo: int
    customer_email: str
    customer_code: str
    channel: str
    paid_at: str
    metadata: dict[str, Any]

    @property
    def amount_ngn(self) -> float:
        """Amount in Naira (Paystack sends kobo, i.e. ₦1 = 100 kobo)."""
        return self.amount_kobo / 100


class PaystackError(Exception):
    """Raised when the Paystack API returns an error."""
    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Paystack API error {status_code}: {message}")


# ── API Client ───────────────────────────────────────────────

class PaystackClient:
    """Async HTTP client for Paystack APIs."""

    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {settings.paystack_secret_key}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        # Build this at request time so runtime environment changes are reflected.
        self._headers["Authorization"] = "Bearer " + settings.paystack_secret_key
        return httpx.AsyncClient(
            base_url=_BASE_URL,
            headers=self._headers,
            timeout=_TIMEOUT,
        )

    # ── Create customer ──────────────────────────────────────

    async def create_customer(
        self,
        email: str,
        first_name: str = "",
        last_name: str = "",
        phone: str = "",
    ) -> str:
        """Create a Paystack customer and return the customer code."""
        payload: dict[str, Any] = {"email": email}
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if phone:
            payload["phone"] = phone

        async with self._client() as client:
            resp = await client.post("/customer", json=payload)

        if resp.status_code not in (200, 201):
            raise PaystackError(resp.status_code, resp.text)

        data = resp.json()
        customer_code = data["data"]["customer_code"]
        logger.info("Created Paystack customer")
        return customer_code

    # ── Create Dedicated Virtual Account ─────────────────────

    async def create_dva(self, customer_code: str) -> DVAInfo:
        """Create a Dedicated Virtual Account for an existing customer.

        Returns the bank account details the user should transfer to.
        """
        payload = {
            "customer": customer_code,
            "preferred_bank": "wema-bank",
        }

        async with self._client() as client:
            resp = await client.post("/dedicated_account", json=payload)

        if resp.status_code not in (200, 201):
            raise PaystackError(resp.status_code, resp.text)

        data = resp.json()["data"]
        dva = DVAInfo(
            account_number=data["account_number"],
            bank_name=data["bank"]["name"],
            account_name=data["account_name"],
            customer_code=customer_code,
        )
        logger.info("Created DVA at %s", dva.bank_name)
        return dva

    # ── Create DVA + customer in one call ────────────────────

    async def assign_dva(
        self,
        email: str,
        first_name: str,
        last_name: str,
        phone: str = "",
    ) -> DVAInfo:
        """Create a customer and assign a DVA in one call."""
        payload: dict[str, Any] = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name or "Customer",
            "preferred_bank": "wema-bank",
            "country": "NG",
        }
        if phone:
            payload["phone"] = phone

        async with self._client() as client:
            resp = await client.post("/dedicated_account/assign", json=payload)

        if resp.status_code not in (200, 201):
            raise PaystackError(resp.status_code, resp.text)

        data = resp.json()["data"]
        dva = DVAInfo(
            account_number=data["account_number"],
            bank_name=data["bank"]["name"],
            account_name=data["account_name"],
            customer_code=data.get("customer", {}).get("customer_code", ""),
        )
        logger.info("Assigned DVA at %s", dva.bank_name)
        return dva

    # ── Initialize transaction (payment link) ──────────────

    async def initialize_transaction(
        self,
        email: str,
        amount_ngn: float,
        reference: str,
        metadata: dict[str, Any] | None = None,
        callback_url: str = "",
        channels: list[str] | None = None,
    ) -> str:
        """Initialize a Paystack transaction and return the payment URL.

        This generates a hosted payment page where the user can pay
        via card, bank transfer, USSD, etc.

        Args:
            email: Customer email
            amount_ngn: Amount in Naira (will be converted to kobo)
            reference: Unique transaction reference
            metadata: Extra data to attach (e.g. order info)
            callback_url: URL to redirect after payment
            channels: Optional payment channels filter

        Returns:
            The authorization_url (payment page link)
        """
        amount_kobo = int(amount_ngn * 100)

        payload: dict[str, Any] = {
            "email": email,
            "amount": amount_kobo,
            "reference": reference,
            "currency": "NGN",
        }
        if channels:
            payload["channels"] = channels
        if metadata:
            payload["metadata"] = metadata
        # Automatically redirect user back to the Telegram bot chat on payment completion
        redirect_url = callback_url or settings.bot_url
        if redirect_url:
            payload["callback_url"] = redirect_url

        logger.info(
            "Paystack initialize start: ref=%s amount_ngn=%.2f amount_kobo=%d "
            "type=%s secret_configured=%s",
            reference,
            amount_ngn,
            amount_kobo,
            (metadata or {}).get("type", "unknown"),
            bool(settings.paystack_secret_key),
        )
        async with self._client() as client:
            resp = await client.post("/transaction/initialize", json=payload)

        if resp.status_code not in (200, 201):
            logger.error(
                "Paystack initialize failed: ref=%s status=%d response=%s",
                reference, resp.status_code, resp.text[:500],
            )
            raise PaystackError(resp.status_code, resp.text)

        try:
            data = resp.json()["data"]
            url = data["authorization_url"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.exception("Paystack initialize malformed response: ref=%s", reference)
            raise PaystackError(resp.status_code, "Malformed Paystack response") from exc
        logger.info("Paystack initialize success: ref=%s", reference)
        return url

    # ── Verify transaction ───────────────────────────────────

    async def verify_transaction(self, reference: str) -> dict[str, Any]:
        """Verify a transaction by reference."""
        logger.debug("Paystack verify start: ref=%s", reference)
        async with self._client() as client:
            resp = await client.get(f"/transaction/verify/{reference}")

        if resp.status_code != 200:
            logger.error(
                "Paystack verify failed: ref=%s status=%d response=%s",
                reference, resp.status_code, resp.text[:500],
            )
            raise PaystackError(resp.status_code, resp.text)

        try:
            data = resp.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.exception("Paystack verify malformed response: ref=%s", reference)
            raise PaystackError(resp.status_code, "Malformed Paystack response") from exc
        logger.debug("Paystack verify result: ref=%s status=%s", reference, data.get("status"))
        return data

    # ── Webhook signature verification ───────────────────────

    @staticmethod
    def verify_webhook_signature(payload: bytes, signature: str) -> bool:
        """Verify that a webhook request is genuinely from Paystack.

        Paystack signs webhooks with HMAC SHA-512 using your secret key.
        """
        if not settings.paystack_secret_key:
            logger.error("Paystack webhook verification is not configured")
            return False

        expected = hmac.new(
            settings.paystack_secret_key.encode("utf-8"),
            payload,
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ── Parse webhook event ──────────────────────────────────

    @staticmethod
    def parse_charge_success(data: dict[str, Any]) -> PaymentEvent | None:
        """Parse a charge.success webhook payload into a PaymentEvent."""
        if data.get("event") != "charge.success":
            return None

        event_data = data.get("data", {})
        customer = event_data.get("customer", {})

        return PaymentEvent(
            reference=event_data.get("reference", ""),
            amount_kobo=int(event_data.get("amount", 0)),
            customer_email=customer.get("email", ""),
            customer_code=customer.get("customer_code", ""),
            channel=event_data.get("channel", ""),
            paid_at=event_data.get("paid_at", ""),
            metadata=event_data.get("metadata", {}),
        )


# ── Module-level singleton ───────────────────────────────────
paystack = PaystackClient()
