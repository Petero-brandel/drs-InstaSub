"""
DRSub – Referral earnings model.

Tracks every referral commission event: which referrer earned
how much from which order by which referred user.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base


class ReferralEarning(Base):
    """Records a single referral commission payout."""

    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The referrer who earns the commission
    referrer_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The referred user whose purchase triggered the commission
    referred_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The order that generated this commission
    order_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Commission amount in NGN (credited to referrer wallet)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # The order total that the commission was calculated on
    order_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Commission rate applied (e.g. 5.0 for 5%)
    rate_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)

    # Product info snapshot
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # Relationships
    referrer = relationship("User", foreign_keys=[referrer_user_id], backref="referral_earnings")
    referred = relationship("User", foreign_keys=[referred_user_id])

    def __repr__(self) -> str:
        return (
            f"<ReferralEarning referrer={self.referrer_user_id} "
            f"referred={self.referred_user_id} ₦{self.amount:,.2f}>"
        )
