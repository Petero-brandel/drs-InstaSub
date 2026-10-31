"""
DRSub – Custom pricing model.

Lets the admin set custom ₦ prices per product.
Users see the admin price; SubExpress is paid at their real rate.
The difference is the admin's profit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from bot.database import Base


class CustomPrice(Base):
    """Admin-defined NGN price for a SubExpress product."""

    __tablename__ = "custom_prices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    price_ngn: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<CustomPrice product_id={self.product_id} ₦{self.price_ngn:,.2f}>"
