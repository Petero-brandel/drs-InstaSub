"""
DRSub – Application entry point.

Initialises the database, registers all handlers, and starts the bot
in long-polling mode (suitable for development).
"""

from __future__ import annotations

import asyncio
import logging

# Python 3.12+ no longer auto-creates an event loop – ensure one exists
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from telegram.ext import ApplicationBuilder

from bot.config import settings
from bot.database import init_db, close_db
from bot.handlers import start, catalog, wallet, purchase, manual_product_admin
from bot.handlers import admin as admin_handler
from bot.webhook import set_bot_app
from bot.services import payment_poller

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    level=logging.INFO,
)
# HTTPX includes full request URLs in INFO logs. Telegram bot URLs contain the
# bot token, so keep HTTP client request logging disabled in production.
for _logger_name in ("httpx", "httpcore"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)
logger = logging.getLogger("drsub")

# ── Module-level app reference (for admin commands etc.) ─────
_app = None


def get_app():
    """Return the running Application instance."""
    return _app


# ── Post-init hook (runs once when the bot starts) ───────────

async def post_init(app) -> None:
    """Called after the Application is initialised but before polling starts."""
    global _app
    _app = app
    logger.info(
        "Runtime configuration: database=%s paystack_configured=%s admin_count=%d",
        settings.database_url.split("://", 1)[0],
        bool(settings.paystack_secret_key),
        len(settings.admin_id_list),
    )

    logger.info("Initialising database …")
    await init_db()
    logger.info("Database ready ✓")

    # Give webhook server and payment poller access to the bot
    set_bot_app(app)
    payment_poller.set_bot_app(app)
    await payment_poller.start_pending_transaction_cleanup()

    # Register bot commands menu
    from bot.handlers.start import setup_commands
    await setup_commands(app)

    # Initialise dynamic forex exchange rates (daily auto-sync)
    from bot.services.forex import init_forex
    await init_forex(app)
    from bot.services.stock_monitor import start_stock_monitor
    await start_stock_monitor(app)


async def post_shutdown(app) -> None:
    """Called when the Application is shutting down."""
    from bot.services.forex import stop_forex
    await stop_forex()
    from bot.services.stock_monitor import stop_stock_monitor
    await stop_stock_monitor()
    await payment_poller.stop_pending_transaction_cleanup()

    logger.info("Closing database connections …")
    await close_db()
    logger.info("Shutdown complete ✓")


# ── Build & run ──────────────────────────────────────────────

def main() -> None:
    """Build the Telegram Application, register handlers, and start polling."""

    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register handler modules
    start.register(app)
    catalog.register(app)
    wallet.register(app)
    purchase.register(app)
    admin_handler.register(app)
    manual_product_admin.register(app)

    # ── Start ────────────────────────────────────────────────
    logger.info("Starting DRSub bot (polling) …")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
