"""
DRSub – Async database engine & session helpers.

Uses SQLAlchemy 2.0 async API with asyncpg driver.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from bot.config import settings

logger = logging.getLogger(__name__)

# ── Engine ────────────────────────────────────────────────────
_is_sqlite = settings.database_url.startswith("sqlite")

_engine_kwargs: dict = dict(echo=False)
if not _is_sqlite:
    _engine_kwargs.update(
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=300,
    )

engine = create_async_engine(settings.database_url, **_engine_kwargs)

# ── Session factory ──────────────────────────────────────────
async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Declarative base ────────────────────────────────────────
class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


# ── Lifecycle helpers ────────────────────────────────────────

async def init_db() -> None:
    """Create all tables that don't exist yet.

    In production you'd use Alembic migrations instead,
    but this is handy during development.
    """
    # Ensure all models are imported so create_all sees them
    import bot.models.referral  # noqa: F401
    import bot.models.exchange_rate  # noqa: F401
    import bot.models.manual_product  # noqa: F401
    import bot.models.reseller  # noqa: F401
    import bot.models.super_reseller  # noqa: F401
    import bot.models.shared_account  # noqa: F401
    import bot.models.reuse_config  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # ── Safe column migrations for existing deployments ──
        from sqlalchemy import text
        _migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT 'NGN'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code VARCHAR(20)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by INTEGER",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_reseller BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_super_reseller BOOLEAN DEFAULT FALSE",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'subexpress'",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS manual_product_id INTEGER",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS shared_account_id INTEGER",
            "ALTER TABLE manual_products ADD COLUMN IF NOT EXISTS delivery_message TEXT DEFAULT 'Login details:\\n{login_details}'",
        ]
        for sql in _migrations:
            try:
                await conn.execute(text(sql))
            except Exception:
                # SQLite doesn't support IF NOT EXISTS – try without
                try:
                    col_name = sql.split("ADD COLUMN IF NOT EXISTS ")[1].split()[0]
                    fallback = sql.replace("IF NOT EXISTS ", "")
                    await conn.execute(text(fallback))
                except Exception:
                    pass  # Column likely already exists

    logger.info("Database schema ready, including shared-account reuse tables")


async def close_db() -> None:
    """Dispose of the connection pool."""
    await engine.dispose()
