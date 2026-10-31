"""
DRSub – Pending order model.

Tracks orders awaiting direct payment (not from wallet).
These have a TTL and expire if not paid within 30 minutes.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
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

# How long a pending order stays alive before expiring
PENDING_ORDER_TTL = timedelta(minutes=30)


class PendingOrder(Base):
    """An order awaiting direct payment via bank transfer.

    Created when a user chooses 'Pay Directly' instead of using
    their wallet. Expires after 30 minutes if not paid.
    """

    __tablename__ = "pending_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Product snapshot
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Expected payment
    amount_ngn: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Status
    is_fulfilled: Mapped[bool] = mapped_column(default=False)
    is_expired: Mapped[bool] = mapped_column(default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc) + PENDING_ORDER_TTL,
    )

    # Relationships
    user = relationship("User", backref="pending_orders")

    @property
    def is_active(self) -> bool:
        """Check if this pending order is still valid."""
        now = datetime.now(timezone.utc)
        expires = self.expires_at.replace(tzinfo=timezone.utc) if self.expires_at.tzinfo is None else self.expires_at
        return not self.is_fulfilled and not self.is_expired and now < expires

    @property
    def time_remaining(self) -> str:
        """Human-readable time remaining."""
        now = datetime.now(timezone.utc)
        expires = self.expires_at.replace(tzinfo=timezone.utc) if self.expires_at.tzinfo is None else self.expires_at
        delta = expires - now
        if delta.total_seconds() <= 0:
            return "Expired"
        minutes = int(delta.total_seconds() // 60)
        return f"{minutes} min"

    def __repr__(self) -> str:
        return (
            f"<PendingOrder id={self.id} product={self.product_name!r} "
            f"₦{self.amount_ngn:,.2f} active={self.is_active}>"
        )
