"""Admin-controlled shared-account reuse policy per product."""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bot.database import Base


class ReuseConfig(Base):
    __tablename__ = "reuse_configs"
    __table_args__ = (UniqueConstraint("product_id", "source", name="uq_reuse_product_source"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), default="subexpress", server_default="subexpress")
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    max_reuses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")