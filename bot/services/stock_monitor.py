"""Periodic SubExpress stock and price change notifications."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.database import async_session
from bot.models.user import User
from bot.services.subexpress import subexpress

logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 150
_task: asyncio.Task | None = None
_snapshot: dict[int, tuple[bool, float]] = {}


async def _broadcast(app, message: str, product_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.is_banned == False))  # noqa: E712
        users = result.scalars().all()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Buy Now",
            callback_data=f"prod:{product_id}",
            api_kwargs={"style": "primary"},
        )
    ]])
    for user in users:
        try:
            await app.bot.send_message(chat_id=user.telegram_id, text=message, reply_markup=keyboard)
        except Exception:
            logger.debug("Stock broadcast failed for user %s", user.telegram_id)


async def _poll_once(app) -> None:
    products = await subexpress.list_products()
    current = {p.id: (p.in_stock, round(p.price_usd, 2)) for p in products}
    if not _snapshot:
        _snapshot.update(current)
        return

    changes: list[tuple[str, object]] = []
    for product in products:
        old = _snapshot.get(product.id)
        new = current[product.id]
        if old is None:
            continue
        if old[0] != new[0]:
            status = "BACK IN STOCK" if new[0] else "OUT OF STOCK"
            message = f"📢 *{product.name}*\n\nStatus update: *{status}*\n\nTap Buy Now to view this product."
            changes.append((message, product.id))
        elif old[1] != new[1]:
            message = f"📢 *Price Update*\n\n{product.name} is now ${product.price_usd:,.2f}.\n\nTap Buy Now to view the latest price."
            changes.append((message, product.id))

    for message, product_id in changes:
        await _broadcast(app, message, product_id)

    # Advance the baseline only after comparing the complete successful poll.
    _snapshot.clear()
    _snapshot.update(current)


async def _scheduler(app) -> None:
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            await _poll_once(app)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Stock monitor poll failed")


async def start_stock_monitor(app) -> None:
    global _task
    if _task is None or _task.done():
        try:
            await _poll_once(app)
        except Exception:
            logger.exception("Initial stock monitor poll failed")
        _task = asyncio.create_task(_scheduler(app))
        logger.info("Stock monitor started (every %ss)", POLL_INTERVAL_SECONDS)


async def stop_stock_monitor() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
