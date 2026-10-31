"""
DRSub – Currency & exchange rate services.

Supports multi-currency display:
- 🇳🇬 NGN (Nigerian Naira) [Paystack Settlement Base]
- 🇬🇭 GHS (Ghanaian Cedi)
- 🇰🇪 KES (Kenyan Shilling)
- 🇿🇦 ZAR (South African Rand)
- 🇺🇸 USD (US Dollar / International)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import settings
from bot.database import async_session
from bot.models.user import User


@dataclass
class CurrencyInfo:
    code: str
    symbol: str
    name: str
    flag: str
    rate_per_usd: float


CURRENCIES: dict[str, CurrencyInfo] = {
    "NGN": CurrencyInfo(code="NGN", symbol="₦", name="Nigeria (Naira)", flag="🇳🇬", rate_per_usd=1346.0),
    "GHS": CurrencyInfo(code="GHS", symbol="GH₵", name="Ghana (Cedi)", flag="🇬🇭", rate_per_usd=11.64),
    "KES": CurrencyInfo(code="KES", symbol="KSh", name="Kenya (Shilling)", flag="🇰🇪", rate_per_usd=129.8),
    "ZAR": CurrencyInfo(code="ZAR", symbol="R", name="South Africa (Rand)", flag="🇿🇦", rate_per_usd=16.6),
    "USD": CurrencyInfo(code="USD", symbol="$", name="International (USD)", flag="🇺🇸", rate_per_usd=1.0),
}


def update_currency_rate(code: str, rate_per_usd: float) -> None:
    """Update in-memory exchange rate for a currency."""
    code_upper = code.upper()
    if code_upper in CURRENCIES:
        CURRENCIES[code_upper].rate_per_usd = float(rate_per_usd)


def get_currency_info(code: str) -> CurrencyInfo:
    """Return CurrencyInfo for code, defaulting to NGN."""
    code_upper = (code or "NGN").upper()
    curr = CURRENCIES.get(code_upper, CURRENCIES["NGN"])
    if curr.code == "NGN":
        return CurrencyInfo(
            code="NGN",
            symbol="₦",
            name="Nigeria (Naira)",
            flag="🇳🇬",
            rate_per_usd=float(settings.usd_to_ngn_rate),
        )
    return curr


def format_price(
    amount_usd: float,
    currency_code: str = "NGN",
    custom_ngn: float | None = None,
) -> str:
    """Format product price in chosen currency with symbol."""
    info = get_currency_info(currency_code)
    ngn_rate = float(settings.usd_to_ngn_rate)

    if info.code == "NGN":
        ngn_val = custom_ngn if custom_ngn is not None else (amount_usd * ngn_rate)
        return f"₦{ngn_val:,.0f}"

    effective_usd = (custom_ngn / ngn_rate) if custom_ngn is not None else amount_usd
    local_val = effective_usd * info.rate_per_usd
    return f"{info.symbol} {local_val:,.2f}"


def format_dual_price(
    amount_usd: float,
    currency_code: str = "NGN",
    custom_ngn: float | None = None,
) -> str:
    """Format price showing local currency, plus NGN conversion if not in NGN."""
    ngn_rate = float(settings.usd_to_ngn_rate)
    ngn_val = custom_ngn if custom_ngn is not None else (amount_usd * ngn_rate)

    if (currency_code or "NGN").upper() == "NGN":
        return f"₦{ngn_val:,.0f}"

    local_display = format_price(amount_usd, currency_code, custom_ngn)
    return f"{local_display} (~₦{ngn_val:,.0f})"


def convert_ngn_to_local(amount_ngn: float, currency_code: str = "NGN") -> tuple[float, str]:
    """Convert an NGN amount to target currency. Returns (value, formatted_string)."""
    info = get_currency_info(currency_code)
    ngn_rate = float(settings.usd_to_ngn_rate)
    effective_usd = amount_ngn / ngn_rate
    local_val = effective_usd * info.rate_per_usd

    if info.code == "NGN":
        return round(amount_ngn), f"₦{amount_ngn:,.0f}"
    return round(local_val, 2), f"{info.symbol} {local_val:,.2f}"


async def get_user_currency(telegram_id: int) -> str:
    """Fetch user's selected currency from DB, default to NGN."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(User.currency).where(User.telegram_id == telegram_id)
            )
            curr = result.scalar_one_or_none()
            if curr and curr.upper() in CURRENCIES:
                return curr.upper()
    except Exception:
        pass
    return "NGN"


async def set_user_currency(telegram_id: int, currency: str) -> bool:
    """Save user's currency preference to DB."""
    code_upper = currency.upper()
    if code_upper not in CURRENCIES:
        return False
    try:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )
            user = result.scalar_one_or_none()
            if user:
                user.currency = code_upper
                await session.commit()
                return True
    except Exception:
        pass
    return False


def build_currency_keyboard(current_code: str = "NGN") -> InlineKeyboardMarkup:
    """Build currency switcher keyboard with checkmark for current."""
    current_upper = (current_code or "NGN").upper()
    buttons: list[list[InlineKeyboardButton]] = []

    for code, info in CURRENCIES.items():
        is_active = code == current_upper
        tag = " ✓" if is_active else ""
        label = f"{info.flag} {info.name} ({info.symbol}){tag}"
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"set_curr:{code}",
                api_kwargs={"style": "success" if is_active else "primary"},
            )
        ])

    buttons.append([
        InlineKeyboardButton("« Back to Shop", callback_data="catalog"),
        InlineKeyboardButton("Main Menu", callback_data="start"),
    ])
    return InlineKeyboardMarkup(buttons)
