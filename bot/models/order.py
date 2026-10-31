"""
DRSub – Order model.

Tracks every purchase made through the bot, linking users to
SubExpress orders and storing the delivered keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base


class OrderStatus(str, PyEnum):
    PENDING = "pending"          # Awaiting payment or processing
    PAID = "paid"                # Payment confirmed, awaiting fulfillment
    FULFILLED = "fulfilled"      # Keys delivered successfully
    FAILED = "failed"            # SubExpress order failed
    REFUNDED = "refunded"        # Payment refunded to wallet


class Order(Base):
    """A purchase order linking a user to a SubExpress fulfillment."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transaction_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=True,
    )

    # Product info (snapshot at time of purchase)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="subexpress", server_default="subexpress")
    manual_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shared_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Pricing
    amount_ngn: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # SubExpress response
    subexpress_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keys: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(
        String(20),
        default=OrderStatus.PENDING.value,
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # Relationships
    user = relationship("User", backref="orders")
    transaction = relationship("Transaction", backref="order", uselist=False)

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} product={self.product_name!r} "
            f"status={self.status} ₦{self.amount_ngn:,.2f}>"
        )
