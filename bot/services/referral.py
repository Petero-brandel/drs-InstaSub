"""
DRSub – Referral program service.

Handles referral code generation, referral tracking, commission
calculation & payout, and statistics queries.
"""

from __future__ import annotations

import logging
import secrets
import string
from decimal import Decimal

from sqlalchemy import select, func, BigInteger

from bot.database import async_session
from bot.models.user import User
from bot.models.wallet import Wallet, Transaction, TransactionType, TransactionStatus
from bot.models.order import Order
from bot.models.referral import ReferralEarning

logger = logging.getLogger(__name__)

# ── Default settings (admin-configurable via bot_data) ───────
REFERRAL_BONUS_NGN = Decimal("500.00")
DEFAULT_COMMISSION_RATE = 0.0      # Retained for compatibility with existing settings.
REFERRAL_CODE_LENGTH = 8


# ── Helpers ──────────────────────────────────────────────────

def _generate_code(length: int = REFERRAL_CODE_LENGTH) -> str:
    """Generate a random alphanumeric referral code (uppercase)."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def get_referral_settings(app_bot_data: dict | None = None) -> dict:
    """Return current referral program settings.

    Settings can be stored in ``app.bot_data`` for persistence
    across restarts (or we fall back to defaults).
    """
    if app_bot_data and "referral_settings" in app_bot_data:
        return app_bot_data["referral_settings"]
    return {
        "enabled": True,
        "commission_rate": DEFAULT_COMMISSION_RATE,
    }


async def save_referral_settings(app_bot_data: dict, settings: dict) -> None:
    """Persist referral settings in bot_data."""
    app_bot_data["referral_settings"] = settings


# ── Code management ──────────────────────────────────────────

async def ensure_referral_code(telegram_id: int) -> str:
    """Return the user's referral code, generating one if needed."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise ValueError("User not found")

        if user.referral_code:
            return user.referral_code

        # Generate unique code
        for _ in range(10):
            code = _generate_code()
            existing = await session.execute(
                select(User).where(User.referral_code == code)
            )
            if existing.scalar_one_or_none() is None:
                user.referral_code = code
                await session.commit()
                return code

        raise RuntimeError("Could not generate unique referral code")


async def get_user_by_referral_code(code: str) -> User | None:
    """Look up a user by their referral code (case-insensitive)."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.referral_code == code.upper())
        )
        return result.scalar_one_or_none()


# ── Referral registration ────────────────────────────────────

async def register_referral(new_user_telegram_id: int, referrer_code: str) -> bool:
    """Link a newly registered user to their referrer.

    Returns True if the referral was successfully recorded,
    False if the code is invalid or the user was already referred.
    """
    async with async_session() as session:
        # Find the referrer
        referrer_result = await session.execute(
            select(User).where(User.referral_code == referrer_code.upper())
        )
        referrer = referrer_result.scalar_one_or_none()
        if not referrer:
            logger.warning("Invalid referral code: %s", referrer_code)
            return False

        # Find the new user
        new_user_result = await session.execute(
            select(User).where(User.telegram_id == new_user_telegram_id)
        )
        new_user = new_user_result.scalar_one_or_none()
        if not new_user:
            return False

        # Cannot refer yourself
        if new_user.id == referrer.id:
            return False

        # Already has a referrer
        if new_user.referred_by:
            return False

        new_user.referred_by = referrer.id
        await session.commit()

        logger.info(
            "Referral recorded: new_user_tg=%d referred_by user_id=%d (code=%s)",
            new_user_telegram_id, referrer.id, referrer_code,
        )
        return True


# ── Commission processing ────────────────────────────────────

async def process_referral_commission(
    order: Order,
    buyer_user_id: int,
    order_amount_ngn: Decimal,
    product_name: str,
    bot_app=None,
    commission_rate: float | None = None,
) -> bool:
    """Calculate and pay referral commission for a fulfilled order.

    Called after an order is successfully delivered (keys sent).
    Returns True if a commission was paid, False otherwise.
    """
    async with async_session() as session:
        # Check if this order already has a referral earning
        existing = await session.execute(
            select(ReferralEarning).where(ReferralEarning.order_id == order.id)
        )
        if existing.scalar_one_or_none():
            return False  # Already processed

        # Find the buyer and check if they were referred
        buyer_result = await session.execute(
            select(User).where(User.id == buyer_user_id)
        )
        buyer = buyer_result.scalar_one_or_none()
        if not buyer or not buyer.referred_by:
            return False

        referrer_id = buyer.referred_by

        # Calculate commission
        commission = REFERRAL_BONUS_NGN

        if commission <= 0:
            return False

        # Credit referrer's wallet
        wallet_result = await session.execute(
            select(Wallet).where(Wallet.user_id == referrer_id)
        )
        wallet = wallet_result.scalar_one_or_none()
        if wallet is None:
            wallet = Wallet(user_id=referrer_id, balance=commission)
            session.add(wallet)
        else:
            wallet.balance += commission

        # Record the commission transaction
        tx = Transaction(
            user_id=referrer_id,
            type=TransactionType.ADMIN_CREDIT.value,
            amount=commission,
            status=TransactionStatus.COMPLETED.value,
            description=f"Referral commission: {product_name}",
        )
        session.add(tx)

        # Record the referral earning
        earning = ReferralEarning(
            referrer_user_id=referrer_id,
            referred_user_id=buyer_user_id,
            order_id=order.id,
            amount=commission,
            order_amount=order_amount_ngn,
            rate_percent=Decimal("0"),
            product_name=product_name,
        )
        session.add(earning)

        await session.commit()

        # Get referrer's telegram_id for notification
        referrer_result = await session.execute(
            select(User).where(User.id == referrer_id)
        )
        referrer = referrer_result.scalar_one_or_none()

        logger.info(
            "Referral commission paid: referrer_id=%d amount=₦%.2f order_id=%d",
            referrer_id, commission, order.id,
        )

    # Notify the referrer
    if bot_app and referrer:
        try:
            await bot_app.bot.send_message(
                chat_id=referrer.telegram_id,
                text=(
                    "🎉 *Referral Bonus!*\n\n"
                    f"You earned *₦{commission:,.2f}* from a referral purchase!\n"
                    f"• Product: _{product_name}_\n"
                    "• Bonus: ₦500 per purchase\n\n"
                    f"💰 The bonus has been credited to your wallet."
                ),
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("Failed to notify referrer %d: %s", referrer.telegram_id, exc)

    return True


# ── Statistics ────────────────────────────────────────────────

async def get_referral_stats(user_id: int) -> dict:
    """Get referral statistics for a user.

    Returns dict with:
      - total_referrals: number of users referred
      - total_earnings: total NGN earned from commissions
      - recent_earnings: list of recent ReferralEarning objects
    """
    async with async_session() as session:
        # Count referred users
        referral_count = await session.scalar(
            select(func.count(User.id)).where(User.referred_by == user_id)
        )

        # Total earnings
        total_earned = await session.scalar(
            select(func.sum(ReferralEarning.amount))
            .where(ReferralEarning.referrer_user_id == user_id)
        ) or Decimal("0.00")

        # Recent earnings (last 10)
        recent_result = await session.execute(
            select(ReferralEarning)
            .where(ReferralEarning.referrer_user_id == user_id)
            .order_by(ReferralEarning.created_at.desc())
            .limit(10)
        )
        recent = list(recent_result.scalars().all())

    return {
        "total_referrals": referral_count or 0,
        "total_earnings": total_earned,
        "recent_earnings": recent,
    }


async def get_top_referrers(limit: int = 10) -> list[dict]:
    """Get top referrers for the admin panel.

    Returns list of dicts with user info and referral stats.
    """
    async with async_session() as session:
        # Get users with most referrals and their earnings
        stmt = (
            select(
                User.id,
                User.telegram_id,
                User.username,
                User.first_name,
                func.count(User.id).label("ref_count"),
            )
            .join(
                User,
                User.referred_by == User.id,
                isouter=False,
            )
        )
        # This query is tricky with self-join, let's use a simpler approach
        # Get all users who have been referred, group by referrer
        from sqlalchemy.orm import aliased
        Referred = aliased(User)

        referrer_stats = await session.execute(
            select(
                User.id,
                User.telegram_id,
                User.username,
                User.first_name,
                func.count(Referred.id).label("ref_count"),
            )
            .join(Referred, Referred.referred_by == User.id)
            .group_by(User.id, User.telegram_id, User.username, User.first_name)
            .order_by(func.count(Referred.id).desc())
            .limit(limit)
        )
        rows = referrer_stats.all()

        results = []
        for row in rows:
            # Get total earnings for this referrer
            total_earned = await session.scalar(
                select(func.sum(ReferralEarning.amount))
                .where(ReferralEarning.referrer_user_id == row.id)
            ) or Decimal("0.00")

            results.append({
                "user_id": row.id,
                "telegram_id": row.telegram_id,
                "username": row.username,
                "first_name": row.first_name,
                "referral_count": row.ref_count,
                "total_earnings": total_earned,
            })

        return results
