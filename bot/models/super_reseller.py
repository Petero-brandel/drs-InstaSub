"""Per-product prices for super-reseller accounts."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bot.database import Base


class SuperResellerPrice(Base):
    __tablename__ = "super_reseller_prices"
    __table_args__ = (
        UniqueConstraint("product_id", "source", name="uq_super_reseller_product_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), default="subexpress", server_default="subexpress")
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    price_ngn: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
