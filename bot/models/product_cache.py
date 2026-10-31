"""
DRSub – Product cache model.

Stores a local copy of SubExpress products so we don't hit the API
on every user interaction.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database import Base


class ProductCache(Base):
    """Locally cached SubExpress product."""

    __tablename__ = "product_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="General")
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    min_qty: Mapped[int] = mapped_column(Integer, default=1)
    max_qty: Mapped[int] = mapped_column(Integer, default=50)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<ProductCache id={self.product_id} name={self.name!r}>"
