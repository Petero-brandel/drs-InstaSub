"""
DRSub – Exchange rate models.

Tracks live and historical exchange rates from trusted financial sources.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database import Base


class ExchangeRate(Base):
    """Stores the latest exchange rate per currency pair."""

    __tablename__ = "exchange_rates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    base_currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)
    target_currency: Mapped[str] = mapped_column(String(10), unique=True, nullable=False, index=True)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(100), default="ExchangeRate-API", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<ExchangeRate 1 {self.base_currency} = {self.rate} {self.target_currency}>"


class ExchangeRateConfig(Base):
    """Configuration for exchange rate conversion (auto vs manual, buffers)."""

    __tablename__ = "exchange_rate_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    is_auto: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    fx_buffer_percent: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    manual_rate_ngn: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_source: Mapped[str | None] = mapped_column(String(100), nullable=True)

    def __repr__(self) -> str:
        return f"<ExchangeRateConfig is_auto={self.is_auto} buffer={self.fx_buffer_percent}%>"
