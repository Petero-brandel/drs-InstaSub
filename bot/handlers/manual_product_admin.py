"""Admin commands for manual products and manual fulfillment."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.config import settings
from bot.database import async_session
from bot.models.manual_product import ManualProduct
from bot.models.order import Order, OrderStatus
from bot.models.user import User

logger = logging.getLogger(__name__)

ADD_NAME, ADD_PRICE, ADD_DESCRIPTION, ADD_DELIVERY_MESSAGE = range(4)


def _admin(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    allowed = bool(user_id and user_id in settings.admin_id_list)
    if not allowed:
        logger.warning(
            "Manual product admin access denied: tg_user=%s admin_ids_configured=%d",
            user_id or "unknown",
            len(settings.admin_id_list),
        )
    return allowed


def _parse_price(value: str) -> Decimal | None:
    try:
        price = Decimal(value.replace(",", "").replace("₦", ""))
    except InvalidOperation:
        return None
    return price if price.is_finite() and price > 0 else None


async def add_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the guided manual-product creation flow."""
    if not _admin(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "Access denied: administrator privileges required."
            )
        return
    context.user_data["manual_product_draft"] = {}
    await update.effective_message.reply_text(
        "Let's add a product.\n\n1/4 Send the product name:"
    )
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _admin(update):
        return ConversationHandler.END
    name = (update.effective_message.text or "").strip()
    if not name:
        await update.effective_message.reply_text("Please send a product name.")
        return ADD_NAME
    context.user_data["manual_product_draft"]["name"] = name
    await update.effective_message.reply_text("2/4 Send the price in NGN (for example: 5000):")
    return ADD_PRICE


async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _admin(update):
        return ConversationHandler.END
    price = _parse_price(update.effective_message.text or "")
    if price is None:
        await update.effective_message.reply_text("Price must be a positive number. Try again:")
        return ADD_PRICE
    context.user_data["manual_product_draft"]["price_ngn"] = price
    await update.effective_message.reply_text(
        "3/4 Send the product description (or send - to leave it empty):"
    )
    return ADD_DESCRIPTION


async def add_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _admin(update):
        return ConversationHandler.END
    description = (update.effective_message.text or "").strip()
    context.user_data["manual_product_draft"]["description"] = "" if description == "-" else description
    await update.effective_message.reply_text(
        "4/4 Send the message the customer should receive after fulfillment.\n\n"
        "Use {login_details} where the login details should appear. "
        "You can also use {product_name} and {order_id}."
    )
    return ADD_DELIVERY_MESSAGE


async def add_delivery_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _admin(update):
        return ConversationHandler.END
    delivery_message = (update.effective_message.text or "").strip()
    if not delivery_message:
        await update.effective_message.reply_text(
            "Please send the customer delivery message:"
        )
        return ADD_DELIVERY_MESSAGE
    draft = context.user_data.pop("manual_product_draft", {})
    try:
        async with async_session() as session:
            product = ManualProduct(
                name=draft["name"],
                price_ngn=draft["price_ngn"],
                description=draft["description"],
                delivery_message=delivery_message,
            )
            session.add(product)
            await session.commit()
            await session.refresh(product)
    except SQLAlchemyError:
        logger.exception(
            "Manual product database insert failed: tg_user=%s name=%r",
            update.effective_user.id if update.effective_user else "unknown",
            draft.get("name", "unknown"),
        )
        await update.effective_message.reply_text(
            "Could not save the manual product. Please try again or contact support."
        )
        return ConversationHandler.END
    logger.info(
        "Manual product created: product_id=%s name=%r price_ngn=%s tg_user=%s",
        product.id,
        product.name,
        product.price_ngn,
        update.effective_user.id if update.effective_user else "unknown",
    )
    await update.effective_message.reply_text(
        f"Manual product #{product.id} created: {product.name} "
        f"(₦{product.price_ngn:,.2f})"
    )
    return ConversationHandler.END


async def cancel_add_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("manual_product_draft", None)
    await update.effective_message.reply_text("Product creation cancelled.")
    return ConversationHandler.END


async def list_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    async with async_session() as session:
        result = await session.execute(select(ManualProduct).order_by(ManualProduct.id))
        products = result.scalars().all()
    if not products:
        await update.effective_message.reply_text("No manual products configured.")
        return
    lines = [f"#{p.id} {p.name} — ₦{p.price_ngn:,.2f} ({'active' if p.in_stock else 'hidden'})" for p in products]
    await update.effective_message.reply_text("Manual products:\n" + "\n".join(lines))


async def delete_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update) or not context.args:
        await update.effective_message.reply_text("Usage: /deletemanual <id>")
        return
    try:
        product_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Product ID must be a number.")
        return
    async with async_session() as session:
        product = await session.get(ManualProduct, product_id)
        if not product:
            await update.effective_message.reply_text("Manual product not found.")
            return
        await session.delete(product)
        await session.commit()
    await update.effective_message.reply_text("Manual product deleted.")


async def edit_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit a product: /editmanual id | Name | price_ngn | description."""
    if not _admin(update):
        return
    parts = [part.strip() for part in " ".join(context.args).split("|", 3)]
    if len(parts) < 3:
        await update.effective_message.reply_text("Usage: /editmanual id | Name | price_ngn | description")
        return
    try:
        product_id = int(parts[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid product ID or price.")
        return
    price = _parse_price(parts[2])
    if price is None or not parts[1]:
        await update.effective_message.reply_text("Invalid product ID or price.")
        return
    async with async_session() as session:
        product = await session.get(ManualProduct, product_id)
        if not product:
            await update.effective_message.reply_text("Manual product not found.")
            return
        product.name = parts[1]
        product.price_ngn = price
        product.description = parts[3] if len(parts) > 3 else product.description
        await session.commit()
    await update.effective_message.reply_text("Manual product updated.")


async def fulfill_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Complete an order: /fulfill <order_id> <delivery text>."""
    if not _admin(update) or len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /fulfill <order_id> <key or delivery text>")
        return
    try:
        order_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Order ID must be a number.")
        return
    delivery = " ".join(context.args[1:])
    async with async_session() as session:
        order = await session.get(Order, order_id)
        if not order or order.source != "manual":
            await update.effective_message.reply_text("Manual order not found.")
            return
        order.status = OrderStatus.FULFILLED.value
        order.keys = [delivery]
        user = await session.get(User, order.user_id)
        product = await session.get(ManualProduct, order.manual_product_id)
        await session.commit()
    try:
        from bot.services.referral import process_referral_commission
        await process_referral_commission(
            order=order,
            buyer_user_id=order.user_id,
            order_amount_ngn=order.amount_ngn,
            product_name=order.product_name,
            bot_app=context.application,
        )
    except Exception:
        pass
    if user:
        await update.effective_message.reply_text(f"Order #{order_id} marked fulfilled.")
        try:
            template = (
                product.delivery_message
                if product and product.delivery_message
                else "Login details:\n{login_details}"
            )
            try:
                customer_message = template.format(
                    login_details=delivery,
                    product_name=order.product_name,
                    order_id=order_id,
                )
            except KeyError:
                logger.warning("Invalid delivery template for manual product %s", order.manual_product_id)
                customer_message = f"Login details:\n{delivery}"
            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=customer_message,
            )
        except Exception:
            pass


def register(app) -> None:
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addmanual", add_manual)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
            ADD_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_description)],
            ADD_DELIVERY_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_delivery_message)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add_manual)],
        allow_reentry=True,
    ))
    app.add_handler(CommandHandler("manuals", list_manual))
    app.add_handler(CommandHandler("deletemanual", delete_manual))
    app.add_handler(CommandHandler("editmanual", edit_manual))
    app.add_handler(CommandHandler("fulfill", fulfill_manual))
