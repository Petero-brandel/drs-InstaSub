"""Reusable credentials fetched from a provider for a product."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database import Base


class SharedAccount(Base):
    __tablename__ = "shared_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), default="subexpress", server_default="subexpress")
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    credentials: Mapped[list | dict] = mapped_column(JSON, nullable=False)
    assigned_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_reuses: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )