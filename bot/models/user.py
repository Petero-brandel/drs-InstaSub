"""
DRSub – User model.

Stores Telegram user info and links to Paystack customer / DVA.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database import Base


class User(Base):
    """Represents a Telegram user who has interacted with the bot."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Paystack integration (populated in Phase 3)
    paystack_customer_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dva_account_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dva_bank_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # User preferences
    currency: Mapped[str] = mapped_column(String(10), default="NGN", server_default="NGN")

    # Referral program
    referral_code: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True, index=True)
    referred_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Admin & moderation
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_reseller: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_super_reseller: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} tg={self.telegram_id} @{self.username}>"
