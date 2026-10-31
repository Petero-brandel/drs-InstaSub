"""Admin-created products fulfilled manually."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database import Base


class ManualProduct(Base):
    __tablename__ = "manual_products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    delivery_message: Mapped[str] = mapped_column(
        Text,
        default=(
            "✅ Your purchase is ready!\n\n"
            "Product: {product_name}\n\n"
            "Login details:\n{login_details}\n\n"
            "Order ID: #{order_id}"
        ),
        server_default="Login details:\n{login_details}",
    )
    price_ngn: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )
