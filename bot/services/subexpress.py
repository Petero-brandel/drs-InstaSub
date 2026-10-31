"""
DRSub – SubExpress Partner API client.

Handles product listing and order placement against the
SubExpress API (https://api.subexpress.space).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

# ── Timeout & retry config ───────────────────────────────────
_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def _strip_emojis(value: str) -> str:
    """Remove common Unicode emoji and presentation characters from a name."""
    cleaned = []
    for character in value:
        codepoint = ord(character)
        is_emoji = (
            0x1F000 <= codepoint <= 0x1FAFF
            or 0x2600 <= codepoint <= 0x27BF
            or codepoint in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299)
            or codepoint in (0x200D, 0x20E3, 0xFE0E, 0xFE0F)
        )
        if not is_emoji:
            cleaned.append(character)
    return " ".join("".join(cleaned).split())


# ── Data classes ─────────────────────────────────────────────

@dataclass
class Product:
    """A product from the SubExpress catalog."""
    id: int
    name: str
    category: str
    price_usd: float
    in_stock: bool
    min_qty: int
    max_qty: int
    description: str
    raw: dict[str, Any]
    custom_price_ngn: float | None = None  # Admin-set price override
    reseller_price_ngn: float | None = None
    source: str = "subexpress"
    manual_product_id: int | None = None

    @property
    def cost_ngn(self) -> float:
        """SubExpress cost in Naira (what you pay them)."""
        return round(self.price_usd * settings.usd_to_ngn_rate, 2)

    @property
    def price_ngn(self) -> float:
        """Selling price in Naira (custom if set, else auto-calculated)."""
        if self.reseller_price_ngn is not None:
            return self.reseller_price_ngn
        if self.custom_price_ngn is not None:
            return self.custom_price_ngn
        return self.cost_ngn

    @property
    def price_ngn_display(self) -> str:
        """Human-readable Naira price like '₦8,500.00'."""
        return f"₦{self.price_ngn:,.2f}"

    @property
    def price_usd_display(self) -> str:
        """Human-readable USD price like '$5.00'."""
        return f"${self.price_usd:,.2f}"


@dataclass(frozen=True)
class OrderResult:
    """Successful order response from SubExpress."""
    order_id: int
    items: list[str]
    charged_usd: float


class OrderError(Enum):
    """Known error codes returned by the SubExpress order endpoint."""
    INVALID_KEY = 401
    UNKNOWN_PRODUCT = 404
    OUT_OF_STOCK = 409
    INSUFFICIENT_BALANCE = 402
    RATE_LIMITED = 429
    BAD_QUANTITY = 400


class SubExpressError(Exception):
    """Raised when the SubExpress API returns an error."""

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        self.order_error = OrderError(status_code) if status_code in OrderError._value2member_map_ else None
        super().__init__(f"SubExpress API error {status_code}: {detail}")

    @property
    def user_message(self) -> str:
        """A user-friendly error message."""
        messages = {
            OrderError.INVALID_KEY: "⚠️ Service configuration error. Please contact admin.",
            OrderError.UNKNOWN_PRODUCT: "❌ This product is no longer available.",
            OrderError.OUT_OF_STOCK: "📦 Sorry, this item is currently out of stock. Nothing was charged.",
            OrderError.INSUFFICIENT_BALANCE: "⚠️ Service balance issue. Please contact admin.",
            OrderError.RATE_LIMITED: "⏳ Too many requests. Please try again in a minute.",
            OrderError.BAD_QUANTITY: "❌ Invalid quantity. Please choose between the allowed range.",
        }
        if self.order_error:
            return messages.get(self.order_error, f"❌ An error occurred (code {self.status_code}).")
        return f"❌ An unexpected error occurred (code {self.status_code})."


# ── API Client ───────────────────────────────────────────────

class SubExpressClient:
    """Async HTTP client for the SubExpress Partner API."""

    def __init__(self) -> None:
        self._base_url = settings.subexpress_base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {settings.subexpress_api_key}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=_TIMEOUT,
        )

    # ── List products ────────────────────────────────────────

    async def list_products(self) -> list[Product]:
        """Fetch all available products from SubExpress.

        Returns a list of Product dataclass instances.
        Raises SubExpressError on API failure.
        """
        async with self._client() as client:
            resp = await client.get("/v1/products")

        if resp.status_code != 200:
            raise SubExpressError(resp.status_code, resp.text)

        data = resp.json()

        # The API may return a list directly or wrap it in a key
        products_raw: list[dict] = data if isinstance(data, list) else data.get("products", data.get("data", []))

        products: list[Product] = []
        for p in products_raw:
            try:
                products.append(
                    Product(
                        id=int(p.get("id", p.get("product_id", 0))),
                        name=_strip_emojis(str(p.get("name", "Unknown"))),
                        category=str(p.get("category", "General")),
                        price_usd=float(p.get("price", p.get("price_usd", 0))),
                        in_stock=bool(p.get("in_stock", p.get("stock", 0))),
                        min_qty=int(p.get("min_qty", 1)),
                        max_qty=int(p.get("max_qty", 50)),
                        description=str(p.get("description", "")),
                        raw=p,
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning("Skipping malformed product: %s – %s", p, exc)

        logger.info("Fetched %d products from SubExpress", len(products))
        return products

    # ── Place order ──────────────────────────────────────────

    async def place_order(self, product_id: int, quantity: int = 1) -> OrderResult:
        """Place an order on SubExpress.

        Returns an OrderResult on success.
        Raises SubExpressError with a descriptive code on failure.
        """
        payload = {"product_id": product_id, "quantity": quantity}

        async with self._client() as client:
            resp = await client.post("/v1/order", json=payload)

        if resp.status_code != 200:
            raise SubExpressError(resp.status_code, resp.text)

        data = resp.json()
        result = OrderResult(
            order_id=data["order_id"],
            items=data.get("items", []),
            charged_usd=float(data.get("charged_usd", 0)),
        )
        logger.info(
            "Order placed: order_id=%d, %d items, charged $%.2f",
            result.order_id,
            len(result.items),
            result.charged_usd,
        )
        return result

    async def get_balance(self) -> float | None:
        """Fetch current partner balance in USD if endpoint is supported."""
        try:
            async with self._client() as client:
                for endpoint in ("/v1/balance", "/v1/user", "/v1/me"):
                    resp = await client.get(endpoint)
                    if resp.status_code == 200:
                        data = resp.json()
                        val = data.get("balance", data.get("balance_usd", data.get("wallet", None)))
                        if val is not None:
                            return float(val)
        except Exception:
            pass
        return None


# ── Module-level singleton ───────────────────────────────────
subexpress = SubExpressClient()
