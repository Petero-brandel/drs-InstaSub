"""
DRSub – Application configuration.

All settings are loaded from environment variables / .env file
via pydantic-settings.  See .env.example for the full list.
"""

from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


# Resolve the project root (two levels up from this file)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Central configuration – populated from .env at startup."""

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Telegram ──────────────────────────────────────────────
    bot_token: str
    admin_ids: str = ""  # comma-separated in .env, e.g. "123,456"
    bot_username: str = "Drs_InstaSub_bot"

    @property
    def bot_url(self) -> str:
        return f"https://t.me/{self.bot_username}"

    @property
    def admin_id_list(self) -> list[int]:
        """Parse admin IDs from comma-separated string."""
        if not self.admin_ids.strip():
            return []
        return [int(x.strip()) for x in self.admin_ids.split(",") if x.strip()]

    def is_admin(self, telegram_id: int) -> bool:
        """Check whether a Telegram user ID is in the admin list."""
        return telegram_id in self.admin_id_list

    # ── Database ──────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./drsub.db"

    # ── SubExpress ────────────────────────────────────────────
    subexpress_api_key: str = ""
    subexpress_base_url: str = "https://api.subexpress.space"

    # ── Paystack ──────────────────────────────────────────────
    paystack_secret_key: str = ""

    # ── Pricing ───────────────────────────────────────────────
    usd_to_ngn_rate: float = 1650.0

    # ── Webhook server ────────────────────────────────────────
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8000
    webhook_base_url: str = ""


# Singleton – import this everywhere
settings = Settings()  # type: ignore[call-arg]
