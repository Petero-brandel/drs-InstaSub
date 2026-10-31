"""
DRSub – Admin handler.

Telegram-based admin panel for managing:
- Product pricing (set custom ₦ prices, keep the margin)
- User management (view, fund, ban)
- Order oversight
- Business stats
- Broadcast messages

Only users whose Telegram IDs are in ADMIN_IDS can use these.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select, func
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.config import settings
from bot.database import async_session
from bot.models.user import User
from bot.models.wallet import Wallet, Transaction, TransactionType, TransactionStatus
from bot.models.order import Order, OrderStatus
from bot.models.custom_price import CustomPrice

logger = logging.getLogger(__name__)

# Conversation states
AWAITING_PRICE = 0
AWAITING_FUND_AMOUNT = 1
AWAITING_BROADCAST = 2
AWAITING_RESELLER_PRICE = 3
AWAITING_REUSE_LIMIT = 4


def _is_admin(user_id: int) -> bool:
    """Check if a user is an admin."""
    return user_id in settings.admin_id_list


# ═══════════════════════════════════════════════════════════════
#  Admin Dashboard
# ═══════════════════════════════════════════════════════════════

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the admin dashboard."""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        if update.effective_message:
            await update.effective_message.reply_text("Access denied: Administrator privileges required.")
        return

    # Gather stats
    async with async_session() as session:
        user_count = await session.scalar(select(func.count(User.id)))
        order_count = await session.scalar(
            select(func.count(Order.id)).where(Order.status == OrderStatus.FULFILLED.value)
        )
        total_revenue = await session.scalar(
            select(func.sum(Order.amount_ngn)).where(Order.status == OrderStatus.FULFILLED.value)
        ) or 0
        pending_orders = await session.scalar(
            select(func.count(Order.id)).where(Order.status.in_([
                OrderStatus.PENDING.value, OrderStatus.PAID.value
            ]))
        )

    # SubExpress partner balance check
    from bot.services.subexpress import subexpress
    subexpress_bal = await subexpress.get_balance()
    sub_text = f"${subexpress_bal:,.2f}" if subexpress_bal is not None else "Connected"
    if subexpress_bal is not None and subexpress_bal < 3.0:
        sub_text += " [Low Balance - Top up at subexpress.space]"

    from bot.services.forex import get_forex_summary
    forex_info = await get_forex_summary()
    rate_ngn = forex_info["active_rate_ngn"]
    mode_str = "Auto" if forex_info["is_auto"] else "Manual"

    text = (
        "*DRSub Admin Console*\n"
        "\n"
        f"• Registered Users: `{user_count}`\n"
        f"• Fulfilled Orders: `{order_count}`\n"
        f"• Pending Orders: `{pending_orders}`\n"
        f"• Total Revenue: *`₦{total_revenue:,.2f}`*\n"
        f"• SubExpress Balance: `{sub_text}`\n"
        f"• USD Exchange Rate: `1 USD = ₦{rate_ngn:,.2f}` ({mode_str})\n"
        "\n"
        "Select a management module:"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Manage Prices", callback_data="admin_prices", api_kwargs={"style": "success"}),
            InlineKeyboardButton("Customer Accounts", callback_data="admin_users", api_kwargs={"style": "primary"}),
        ],
        [
            InlineKeyboardButton("Order Management", callback_data="admin_orders"),
            InlineKeyboardButton("Financial Stats", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("Customer Transactions", callback_data="admin_transactions:0"),
        ],
        [
            InlineKeyboardButton("Referral Program", callback_data="admin_referrals"),
            InlineKeyboardButton("Broadcast Message", callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton("Reseller Accounts", callback_data="admin_resellers"),
            InlineKeyboardButton("Manual Products", callback_data="admin_manuals"),
        ],
        [
            InlineKeyboardButton("Shared Account Reuse", callback_data="admin_reuse"),
        ],
        [
            InlineKeyboardButton("💱 Exchange Rates (Forex)", callback_data="admin_rates"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="start"),
        ],
    ])

    query = update.callback_query
    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ═══════════════════════════════════════════════════════════════
#  Price Management
# ═══════════════════════════════════════════════════════════════

ADMIN_PRICE_PAGE_SIZE = 6


async def _render_edit_pane(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    product_id: int,
    alert_text: str | None = None,
) -> None:
    """Render the high-aesthetic dedicated Edit Pane for a single product."""
    from bot.handlers.catalog import _get_products
    try:
        products = await _get_products()
        product = next((p for p in products if p.id == product_id), None)
    except Exception:
        product = None

    if not product:
        if update.callback_query:
            await update.callback_query.answer("Product not found", show_alert=True)
        return

    # Check database for custom price
    async with async_session() as session:
        result = await session.execute(
            select(CustomPrice).where(CustomPrice.product_id == product_id)
        )
        custom_row = result.scalar_one_or_none()

    cost_ngn = Decimal(str(product.cost_ngn))
    selling_price = custom_row.price_ngn if custom_row else cost_ngn
    margin = selling_price - cost_ngn

    # Save to user context
    context.user_data["edit_product_id"] = product.id
    context.user_data["edit_product_name"] = product.name
    context.user_data["edit_product_cost"] = product.cost_ngn

    text = (
        "*Edit Price & Profit Margin*\n"
        "\n"
        f"• Product: *{product.name}*\n"
        f"• Category: {product.category}\n"
        f"• SubExpress ID: `#{product.id}`\n\n"
        "Financial Breakdown:\n"
        f"• SubExpress Wholesale: `₦{cost_ngn:,.2f}` (debited to you)\n"
        f"• Customer Selling Price: *`₦{selling_price:,.2f}`* (customer pays)\n"
        f"• Net Profit Margin: *`+₦{margin:,.2f}`* per unit\n"
        "\n"
        "One-Tap Margin Presets:\n"
        "Tap a preset button below or reply with any price number in chat:"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("+₦1,000 Margin", callback_data=f"price_preset:{product.id}:1000"),
            InlineKeyboardButton("+₦2,000 Margin", callback_data=f"price_preset:{product.id}:2000"),
        ],
        [
            InlineKeyboardButton("+₦3,000 Margin", callback_data=f"price_preset:{product.id}:3000"),
            InlineKeyboardButton("+₦5,000 Margin", callback_data=f"price_preset:{product.id}:5000"),
        ],
        [
            InlineKeyboardButton("2x Wholesale Cost", callback_data=f"price_preset:{product.id}:2x"),
            InlineKeyboardButton("Reset to Wholesale", callback_data=f"price_preset:{product.id}:reset", api_kwargs={"style": "danger"}),
        ],
        [
            InlineKeyboardButton("Back to Price List", callback_data="admin_prices", api_kwargs={"style": "primary"}),
            InlineKeyboardButton("Admin Console", callback_data="admin_back"),
        ],
    ])

    query = update.callback_query
    if query:
        if alert_text:
            await query.answer(alert_text, show_alert=True)
        else:
            await query.answer()
        if query.message:
            await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def prices_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show paginated products list with wholesale cost, customer selling price, and profit margin."""
    query = update.callback_query
    if query:
        await query.answer()

    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    # Parse page from query data if available: "admin_price_page:<page>"
    page = 0
    if query and query.data and query.data.startswith("admin_price_page:"):
        try:
            page = int(query.data.split(":")[1])
        except (IndexError, ValueError):
            page = 0
    else:
        page = context.user_data.get("admin_price_page", 0)

    context.user_data["admin_price_page"] = page

    from bot.handlers.catalog import _get_products
    try:
        products = await _get_products()
    except Exception:
        msg = query.message if query else update.effective_message
        if msg:
            await msg.edit_text("Could not load products.")
        return

    total_products = len(products)
    total_pages = max(1, (total_products + ADMIN_PRICE_PAGE_SIZE - 1) // ADMIN_PRICE_PAGE_SIZE)
    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0

    start_idx = page * ADMIN_PRICE_PAGE_SIZE
    end_idx = start_idx + ADMIN_PRICE_PAGE_SIZE
    page_products = products[start_idx:end_idx]

    # Get custom prices
    async with async_session() as session:
        result = await session.execute(select(CustomPrice))
        custom_prices = {cp.product_id: cp.price_ngn for cp in result.scalars().all()}

    lines = []
    edit_buttons: list[list[InlineKeyboardButton]] = []

    for p in page_products:
        custom = custom_prices.get(p.id)
        cost = Decimal(str(p.cost_ngn))
        if custom:
            margin = custom - cost
            lines.append(
                f"*{p.id}. {p.name}*\n"
                f"   ├ Wholesale: `₦{cost:,.2f}` (debited to you)\n"
                f"   ├ Customer:  *₦{custom:,.2f}* (customer pays)\n"
                f"   └ Margin:    *+₦{margin:,.2f}* / unit"
            )
            btn_label = f"#{p.id} {p.name[:20]} — ₦{custom:,.0f}"
        else:
            lines.append(
                f"*{p.id}. {p.name}*\n"
                f"   ├ Wholesale: `₦{cost:,.2f}` (debited to you)\n"
                f"   ├ Customer:  _Default ₦{cost:,.2f}_\n"
                f"   └ Margin:    ₦0.00 (At cost)"
            )
            btn_label = f"#{p.id} {p.name[:20]} — Set Price"

        edit_buttons.append([InlineKeyboardButton(btn_label, callback_data=f"setprice_select:{p.id}")])

    # Pagination navigation row
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("Prev", callback_data=f"admin_price_page:{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if end_idx < total_products:
        nav_row.append(InlineKeyboardButton("Next", callback_data=f"admin_price_page:{page + 1}"))

    if nav_row:
        edit_buttons.append(nav_row)

    edit_buttons.append([InlineKeyboardButton("Admin Console", callback_data="admin_back")])

    text = (
        "*Price & Profit Manager*\n"
        "\n"
        f"Page {page + 1} of {total_pages} • {total_products} Subscriptions\n"
        "SubExpress charges your partner wallet the wholesale rate.\n"
        "You keep 100% of the customer retail margin.\n"
        "\n"
        + "\n\n".join(lines)
        + "\n\n"
        "Select a subscription below to edit its price:"
    )

    keyboard = InlineKeyboardMarkup(edit_buttons)
    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def setprice_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt admin for new selling price when tapping an edit button."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END

    if not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    parts = query.data.split(":")
    if len(parts) < 2:
        return ConversationHandler.END

    product_id = int(parts[1])
    await _render_edit_pane(update, context, product_id)
    return AWAITING_PRICE


async def setprice_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle one-tap margin preset buttons in the Edit Pane."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END

    if not update.effective_user or not _is_admin(update.effective_user.id):
        await query.answer("❌ Admin only", show_alert=True)
        return ConversationHandler.END

    parts = query.data.split(":")
    if len(parts) < 3:
        return ConversationHandler.END

    product_id = int(parts[1])
    preset_type = parts[2]

    from bot.handlers.catalog import _get_products
    try:
        products = await _get_products()
        product = next((p for p in products if p.id == product_id), None)
    except Exception:
        product = None

    if not product:
        await query.answer("❌ Product not found", show_alert=True)
        return ConversationHandler.END

    cost_ngn = Decimal(str(product.cost_ngn))
    new_price: Decimal | None = None

    if preset_type == "reset":
        async with async_session() as session:
            result = await session.execute(
                select(CustomPrice).where(CustomPrice.product_id == product_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                await session.delete(existing)
                await session.commit()
        await _render_edit_pane(update, context, product_id, alert_text="Reset to wholesale cost")
        return AWAITING_PRICE
    elif preset_type == "2x":
        new_price = round(cost_ngn * 2, 0)
    else:
        try:
            markup = Decimal(preset_type)
            new_price = round(cost_ngn + markup, 0)
        except Exception:
            return AWAITING_PRICE

    async with async_session() as session:
        result = await session.execute(
            select(CustomPrice).where(CustomPrice.product_id == product_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.price_ngn = new_price
            existing.product_name = product.name
        else:
            session.add(CustomPrice(
                product_id=product_id,
                product_name=product.name,
                price_ngn=new_price,
            ))
        await session.commit()

    margin = new_price - cost_ngn
    await _render_edit_pane(
        update, context, product_id,
        alert_text=f"Price updated to ₦{new_price:,.2f} (+₦{margin:,.2f} profit)"
    )
    return AWAITING_PRICE


async def setprice_receive_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the manually typed price from admin in chat and save it."""
    if not update.effective_message or not update.effective_user:
        return ConversationHandler.END

    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    raw_text = (update.effective_message.text or "").strip().replace(",", "").replace("₦", "")
    if raw_text.lower() in ("/cancel", "cancel", "back"):
        context.user_data.pop("edit_product_id", None)
        context.user_data.pop("edit_product_name", None)
        context.user_data.pop("edit_product_cost", None)
        await prices_callback(update, context)
        return ConversationHandler.END

    try:
        new_price = Decimal(raw_text)
    except Exception:
        await update.effective_message.reply_text(
            "Please enter a valid price number (e.g. `10000`) or tap a preset button above."
        )
        return AWAITING_PRICE

    if new_price <= 0:
        await update.effective_message.reply_text(
            "Price must be greater than zero. Try again or send /cancel."
        )
        return AWAITING_PRICE

    product_id = context.user_data.get("edit_product_id")
    if not product_id:
        await prices_callback(update, context)
        return ConversationHandler.END

    product_name = context.user_data.get("edit_product_name", f"Product #{product_id}")
    cost_ngn = Decimal(str(context.user_data.get("edit_product_cost", 0)))

    async with async_session() as session:
        result = await session.execute(
            select(CustomPrice).where(CustomPrice.product_id == product_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.price_ngn = new_price
            existing.product_name = product_name
        else:
            session.add(CustomPrice(
                product_id=product_id,
                product_name=product_name,
                price_ngn=new_price,
            ))
        await session.commit()

    margin = new_price - cost_ngn
    await update.effective_message.reply_text(
        f"*Price Successfully Updated*\n"
        "\n"
        f"• Product: *{product_name}*\n"
        f"• SubExpress Wholesale: `₦{cost_ngn:,.2f}` (debited partner rate)\n"
        f"• Customer Selling Price: *`₦{new_price:,.2f}`* (amount charged)\n"
        f"• Net Profit Margin: *`+₦{margin:,.2f}`* per unit\n"
        "\n"
        f"Customers will now see and pay *₦{new_price:,.2f}*.",
        parse_mode="Markdown",
    )

    await _render_edit_pane(update, context, product_id)
    return AWAITING_PRICE


async def setprice_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exit price editing and return to prices list."""
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data.pop("edit_product_id", None)
    context.user_data.pop("edit_product_name", None)
    context.user_data.pop("edit_product_cost", None)
    await prices_callback(update, context)
    return ConversationHandler.END


async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a custom price: /setprice <product_id> <price_ngn>"""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        if update.effective_message:
            await update.effective_message.reply_text("Access denied: Administrator privileges required.")
        return

    if not context.args or len(context.args) < 2:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Usage: `/setprice <product_id> <price_ngn>`\n"
                "Example: `/setprice 1 10000`",
                parse_mode="Markdown",
            )
        return

    try:
        product_id = int(context.args[0])
        price_ngn = Decimal(context.args[1].replace(",", "").replace("₦", ""))
    except (ValueError, IndexError):
        if update.effective_message:
            await update.effective_message.reply_text("Invalid product ID or price.")
        return

    if price_ngn <= 0:
        if update.effective_message:
            await update.effective_message.reply_text("Price must be positive.")
        return

    # Find product name & wholesale cost
    from bot.handlers.catalog import _get_products
    try:
        products = await _get_products()
        product = next((p for p in products if p.id == product_id), None)
    except Exception:
        product = None

    product_name = product.name if product else f"Product #{product_id}"
    cost_price = Decimal(str(product.cost_ngn)) if product else Decimal("0")

    async with async_session() as session:
        result = await session.execute(
            select(CustomPrice).where(CustomPrice.product_id == product_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.price_ngn = price_ngn
            existing.product_name = product_name
        else:
            cp = CustomPrice(
                product_id=product_id,
                product_name=product_name,
                price_ngn=price_ngn,
            )
            session.add(cp)

        await session.commit()

    margin = price_ngn - cost_price
    if update.effective_message:
        await update.effective_message.reply_text(
            f"*Price Updated*\n"
            "\n"
            f"• Product: *{product_name}*\n"
            f"• SubExpress Wholesale: `₦{cost_price:,.2f}` (debited partner rate)\n"
            f"• Customer Selling Price: *`₦{price_ngn:,.2f}`* (amount charged)\n"
            f"• Net Profit Margin: *`+₦{margin:,.2f}`* per unit\n"
            "\n"
            f"Customers will now see and pay *₦{price_ngn:,.2f}*.",
            parse_mode="Markdown",
        )


async def resetprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset a custom price: /resetprice <product_id>"""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Usage: `/resetprice <product_id>`", parse_mode="Markdown"
            )
        return

    try:
        product_id = int(context.args[0])
    except ValueError:
        if update.effective_message:
            await update.effective_message.reply_text("Invalid product ID.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(CustomPrice).where(CustomPrice.product_id == product_id)
        )
        cp = result.scalar_one_or_none()
        if cp:
            await session.delete(cp)
            await session.commit()
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"Custom price for product #{product_id} has been reset to wholesale."
                )
        else:
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"No custom price was configured for product #{product_id}."
                )


# ═══════════════════════════════════════════════════════════════
#  User Management
# ═══════════════════════════════════════════════════════════════

async def users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all users with their wallet balances."""
    query = update.callback_query
    if query:
        await query.answer()

    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    async with async_session() as session:
        users_result = await session.execute(
            select(User).order_by(User.created_at.desc()).limit(20)
        )
        users = users_result.scalars().all()

        lines = []
        for u in users:
            wallet_result = await session.execute(
                select(Wallet).where(Wallet.user_id == u.id)
            )
            wallet = wallet_result.scalar_one_or_none()
            balance = wallet.balance if wallet else Decimal("0")
            banned = " [Banned]" if u.is_banned else ""

            name = u.username or u.first_name or f"TG#{u.telegram_id}"
            lines.append(
                f"• *{name}* (ID: `{u.telegram_id}`){banned}\n"
                f"  Balance: ₦{balance:,.2f}"
            )

    header = "DRSub — Customer Directory"
    divider = ""
    users_body = "\n\n".join(lines) if lines else "_No registered users found._"

    text = (
        f"*{header}*\n"
        f"{divider}\n"
        f"Recent registered customers ({len(users)}):\n\n"
        f"{users_body}\n\n"
        f"{divider}\n"
        f"*Quick Operations:*\n"
        f"• `/fund <tg_id> <amount>` — Credit user wallet\n"
        f"• `/debit <tg_id> <amount>` — Debit user wallet\n"
        f"• `/ban <tg_id>` — Restrict user access\n"
        f"• `/unban <tg_id>` — Restore user access"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Admin Console", callback_data="admin_back", api_kwargs={"style": "primary"})],
    ])

    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def fund_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fund a user's wallet: /fund <tg_id> <amount>"""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    if not context.args or len(context.args) < 2:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Usage: `/fund <telegram_id> <amount>`", parse_mode="Markdown"
            )
        return

    try:
        tg_id = int(context.args[0])
        amount = Decimal(context.args[1])
    except (ValueError, IndexError):
        if update.effective_message:
            await update.effective_message.reply_text("Invalid Telegram ID or amount.")
        return

    if amount <= 0:
        if update.effective_message:
            await update.effective_message.reply_text("Amount must be greater than zero.")
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if not user:
            if update.effective_message:
                await update.effective_message.reply_text(f"User `{tg_id}` not found.", parse_mode="Markdown")
            return

        # Credit wallet
        wallet_result = await session.execute(
            select(Wallet).where(Wallet.user_id == user.id)
        )
        wallet = wallet_result.scalar_one_or_none()
        if wallet is None:
            wallet = Wallet(user_id=user.id, balance=amount)
            session.add(wallet)
        else:
            wallet.balance += amount

        # Record transaction
        tx = Transaction(
            user_id=user.id,
            type=TransactionType.ADMIN_CREDIT.value,
            amount=amount,
            status=TransactionStatus.COMPLETED.value,
            description=f"Admin credit by {update.effective_user.id}",
        )
        session.add(tx)
        await session.commit()

    if update.effective_message:
        await update.effective_message.reply_text(
            f"*Wallet Credited*\n"
            "\n"
            f"• Target User: `{tg_id}`\n"
            f"• Amount Added: *₦{amount:,.2f}*\n"
            f"• New Balance: *₦{wallet.balance:,.2f}*\n"
            "",
            parse_mode="Markdown",
        )

    # Notify the user
    try:
        from bot.main import get_app
        app = get_app()
        if app:
            await app.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"*Wallet Credited*\n"
                    "\n"
                    f"Your DRSub account has been credited.\n\n"
                    f"• Amount Added: *₦{amount:,.2f}*\n"
                    f"• Updated Balance: *₦{wallet.balance:,.2f}*\n"
                    "\n"
                    f"Use /catalog to browse subscriptions."
                ),
                parse_mode="Markdown",
            )
    except Exception:
        pass


async def debit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debit a user's wallet: /debit <tg_id> <amount>"""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    if not context.args or len(context.args) < 2:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Usage: `/debit <telegram_id> <amount>`", parse_mode="Markdown"
            )
        return

    try:
        tg_id = int(context.args[0])
        amount = Decimal(context.args[1])
    except (ValueError, IndexError):
        if update.effective_message:
            await update.effective_message.reply_text("Invalid Telegram ID or amount.")
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if not user:
            if update.effective_message:
                await update.effective_message.reply_text(f"User `{tg_id}` not found.", parse_mode="Markdown")
            return

        wallet_result = await session.execute(
            select(Wallet).where(Wallet.user_id == user.id)
        )
        wallet = wallet_result.scalar_one_or_none()
        if not wallet or wallet.balance < amount:
            if update.effective_message:
                await update.effective_message.reply_text("Insufficient wallet balance.")
            return

        wallet.balance -= amount
        tx = Transaction(
            user_id=user.id,
            type=TransactionType.ADMIN_DEBIT.value,
            amount=amount,
            status=TransactionStatus.COMPLETED.value,
            description=f"Admin debit by {update.effective_user.id}",
        )
        session.add(tx)
        await session.commit()

    if update.effective_message:
        await update.effective_message.reply_text(
            f"*Wallet Debited*\n"
            "\n"
            f"• Target User: `{tg_id}`\n"
            f"• Amount Debited: *₦{amount:,.2f}*\n"
            f"• New Balance: *₦{wallet.balance:,.2f}*\n"
            "",
            parse_mode="Markdown",
        )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ban a user: /ban <tg_id>"""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: `/ban <telegram_id>`", parse_mode="Markdown")
        return

    tg_id = int(context.args[0])
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if user:
            user.is_banned = True
            await session.commit()
            if update.effective_message:
                await update.effective_message.reply_text(f"User `{tg_id}` restricted.", parse_mode="Markdown")
        else:
            if update.effective_message:
                await update.effective_message.reply_text(f"User `{tg_id}` not found.", parse_mode="Markdown")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unban a user: /unban <tg_id>"""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    if not context.args:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: `/unban <telegram_id>`", parse_mode="Markdown")
        return

    tg_id = int(context.args[0])
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if user:
            user.is_banned = False
            await session.commit()
            if update.effective_message:
                await update.effective_message.reply_text(f"User `{tg_id}` unbanned.", parse_mode="Markdown")
        else:
            if update.effective_message:
                await update.effective_message.reply_text(f"User `{tg_id}` not found.", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
#  Orders Overview
# ═══════════════════════════════════════════════════════════════

async def orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent orders to admin."""
    query = update.callback_query
    if query:
        await query.answer()

    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    async with async_session() as session:
        orders_result = await session.execute(
            select(Order).order_by(Order.created_at.desc()).limit(15)
        )
        orders = orders_result.scalars().all()

    header = "DRSub — Order Management"
    divider = ""

    if not orders:
        text = f"*{header}*\n{divider}\n_No orders placed yet._"
    else:
        lines = []
        for o in orders:
            status_tag = {
                OrderStatus.FULFILLED.value: "[Delivered]",
                OrderStatus.PENDING.value: "[Pending]",
                OrderStatus.PAID.value: "[Processing]",
                OrderStatus.FAILED.value: "[Failed]",
                OrderStatus.REFUNDED.value: "[Refunded]",
            }.get(o.status, f"[{o.status.title()}]")

            lines.append(
                f"• *Order #{o.id}* — {status_tag}\n"
                f"  Item: *{o.product_name}* (Qty: {o.quantity})\n"
                f"  Amount: ₦{o.amount_ngn:,.2f} | User: `{o.user_id}`\n"
                f"  Date: {o.created_at.strftime('%d %b %Y, %H:%M')}"
            )

        text = (
            f"*{header}*\n"
            f"{divider}\n"
            f"Recent customer orders ({len(orders)}):\n\n"
            + "\n\n".join(lines)
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Admin Console", callback_data="admin_back", api_kwargs={"style": "primary"})],
    ])

    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ═══════════════════════════════════════════════════════════════
#  Detailed Stats
# ═══════════════════════════════════════════════════════════════

TRANSACTION_PAGE_SIZE = 8


async def transactions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show customer transactions and payment totals to admin."""
    query = update.callback_query
    if query:
        await query.answer()

    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    page = 0
    if query and query.data:
        parts = query.data.split(":")
        if len(parts) > 1:
            try:
                page = max(0, int(parts[1]))
            except ValueError:
                page = 0

    async with async_session() as session:
        successful_count = await session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.status == TransactionStatus.COMPLETED.value
            )
        ) or 0
        successful_amount = await session.scalar(
            select(func.sum(Transaction.amount)).where(
                Transaction.status == TransactionStatus.COMPLETED.value
            )
        ) or Decimal("0")
        pending_count = await session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.status == TransactionStatus.PENDING.value
            )
        ) or 0
        failed_count = await session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.status == TransactionStatus.FAILED.value
            )
        ) or 0
        deposit_amount = await session.scalar(
            select(func.sum(Transaction.amount)).where(
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.status == TransactionStatus.COMPLETED.value,
            )
        ) or Decimal("0")
        purchase_amount = await session.scalar(
            select(func.sum(Transaction.amount)).where(
                Transaction.type == TransactionType.PURCHASE.value,
                Transaction.status == TransactionStatus.COMPLETED.value,
            )
        ) or Decimal("0")

        rows_result = await session.execute(
            select(Transaction, User)
            .join(User, User.id == Transaction.user_id)
            .where(Transaction.status == TransactionStatus.COMPLETED.value)
            .order_by(Transaction.created_at.desc(), Transaction.id.desc())
            .offset(page * TRANSACTION_PAGE_SIZE)
            .limit(TRANSACTION_PAGE_SIZE)
        )
        rows = rows_result.all()

    if not rows and page > 0:
        page = 0
        async with async_session() as session:
            rows_result = await session.execute(
                select(Transaction, User)
                .join(User, User.id == Transaction.user_id)
                .where(Transaction.status == TransactionStatus.COMPLETED.value)
                .order_by(Transaction.created_at.desc(), Transaction.id.desc())
                .limit(TRANSACTION_PAGE_SIZE)
            )
            rows = rows_result.all()

    lines = []
    for transaction, user in rows:
        customer = f"@{user.username}" if user.username else str(user.telegram_id)
        status = transaction.status.title()
        transaction_type = transaction.type.replace("_", " ").title()
        reference = transaction.reference or "—"
        created_at = transaction.created_at.strftime("%d %b %Y, %H:%M")
        lines.append(
            f"• *#{transaction.id}* {transaction_type} — `{status}`\n"
            f"  Customer: `{customer}` | Amount: *₦{transaction.amount:,.2f}*\n"
            f"  Ref: `{reference}` | {created_at}"
        )

    total_pages = max(1, (successful_count + TRANSACTION_PAGE_SIZE - 1) // TRANSACTION_PAGE_SIZE)
    text = (
        "*DRSub — Customer Transactions*\n\n"
        "*Totals*\n"
        f"• All successful transactions: *{successful_count}* · *₦{Decimal(str(successful_amount)):,.2f}*\n"
        f"• Pending: *{pending_count}* · Failed: *{failed_count}*\n"
        f"• Deposits completed: *₦{Decimal(str(deposit_amount)):,.2f}*\n"
        f"• Purchases completed: *₦{Decimal(str(purchase_amount)):,.2f}*\n\n"
        f"*Recent successful customer transactions — page {page + 1}/{total_pages}*\n\n"
        + ("\n\n".join(lines) if lines else "_No customer transactions yet._")
    )

    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("Previous", callback_data=f"admin_transactions:{page - 1}"))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton("Next", callback_data=f"admin_transactions:{page + 1}"))

    keyboard_rows = [navigation] if navigation else []
    keyboard_rows.extend([
        [InlineKeyboardButton("Refresh", callback_data=f"admin_transactions:{page}")],
        [InlineKeyboardButton("Admin Console", callback_data="admin_back", api_kwargs={"style": "primary"})],
    ])
    keyboard = InlineKeyboardMarkup(keyboard_rows)

    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed business stats."""
    query = update.callback_query
    if query:
        await query.answer()

    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    async with async_session() as session:
        user_count = await session.scalar(select(func.count(User.id)))

        # Revenue
        total_revenue = await session.scalar(
            select(func.sum(Order.amount_ngn)).where(Order.status == OrderStatus.FULFILLED.value)
        ) or Decimal("0")

        total_cost_usd = await session.scalar(
            select(func.sum(Order.amount_usd)).where(Order.status == OrderStatus.FULFILLED.value)
        ) or Decimal("0")

        total_cost_ngn = Decimal(str(total_cost_usd)) * Decimal(str(settings.usd_to_ngn_rate))
        total_profit = Decimal(str(total_revenue)) - total_cost_ngn

        # Orders
        fulfilled = await session.scalar(
            select(func.count(Order.id)).where(Order.status == OrderStatus.FULFILLED.value)
        ) or 0
        failed = await session.scalar(
            select(func.count(Order.id)).where(Order.status == OrderStatus.FAILED.value)
        ) or 0
        total_orders = (fulfilled or 0) + (failed or 0)

        # Deposits
        total_deposits = await session.scalar(
            select(func.sum(Transaction.amount)).where(
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.status == TransactionStatus.COMPLETED.value,
            )
        ) or Decimal("0")

    header = "DRSub — Financial & Business Analytics"
    divider = ""
    text = (
        f"*{header}*\n"
        f"{divider}\n"
        f"*Customer Base*\n"
        f"• Total Registered Customers: *{user_count}*\n\n"
        f"*Revenue & Margins*\n"
        f"• Gross Customer Sales: *₦{Decimal(str(total_revenue)):,.2f}*\n"
        f"• SubExpress Wholesale Cost: ₦{total_cost_ngn:,.2f}\n"
        f"• Net Margin Profit: *₦{total_profit:,.2f}*\n\n"
        f"*Order Operations*\n"
        f"• Total Order Volume: *{total_orders}*\n"
        f"• Successfully Delivered: *{fulfilled}*\n"
        f"• Failed / Refunded: *{failed}*\n\n"
        f"*Customer Deposits*\n"
        f"• Total Wallet Deposits: *₦{Decimal(str(total_deposits)):,.2f}*\n"
        f"{divider}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Admin Console", callback_data="admin_back", api_kwargs={"style": "primary"})],
    ])

    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ═══════════════════════════════════════════════════════════════
#  Broadcast
# ═══════════════════════════════════════════════════════════════

async def _execute_broadcast(
    message_text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Send broadcast message to all active users with Markdown fallback."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.is_banned == False)  # noqa: E712
        )
        users = result.scalars().all()

    msg = update.effective_message
    if not users:
        if msg:
            await msg.reply_text("No active users found to broadcast to.")
        return

    status_msg = None
    if msg:
        status_msg = await msg.reply_text(f"Broadcasting to {len(users)} users...")

    sent = 0
    failed = 0

    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=(
                    f"*Announcement*\n"
                    "\n"
                    f"{message_text}\n"
                    "\n"
                    f"_DRSub Notifications_"
                ),
                parse_mode="Markdown",
            )
            sent += 1
        except Exception:
            # Fallback to plain text if Markdown parsing fails (e.g. unclosed _, *, [)
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"Announcement:\n\n{message_text}",
                )
                sent += 1
            except Exception as exc:
                logger.warning("Failed broadcast to %d: %s", user.telegram_id, exc)
                failed += 1

    report = (
        f"*Broadcast Status Report*\n"
        "\n"
        f"• Delivered: *{sent}*\n"
        f"• Failed / Blocked: *{failed}*\n"
        f"• Target Audience: *{len(users)}*\n"
        ""
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Admin Console", callback_data="admin_back", api_kwargs={"style": "primary"})],
    ])

    if status_msg:
        await status_msg.edit_text(report, parse_mode="Markdown", reply_markup=keyboard)
    elif msg:
        await msg.reply_text(report, parse_mode="Markdown", reply_markup=keyboard)


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start broadcast flow or send immediately if args are provided."""
    query = update.callback_query
    if query:
        await query.answer()

    if not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    # If message was provided directly with command: /broadcast <text>
    if context.args:
        message_text = " ".join(context.args).strip()
        if message_text:
            await _execute_broadcast(message_text, update, context)
            return ConversationHandler.END

    text = (
        "*Broadcast Announcement*\n"
        "\n"
        "Please reply with the message you want to broadcast to all registered customers.\n\n"
        "Direct shortcut: `/broadcast <your message>`\n"
        ""
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Cancel", callback_data="admin_broadcast_cancel", api_kwargs={"style": "danger"})],
    ])

    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    return AWAITING_BROADCAST


async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the broadcast message to all users."""
    if not update.effective_message or not update.effective_user:
        return ConversationHandler.END

    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    message_text = (update.effective_message.text or "").strip()
    if not message_text:
        await update.effective_message.reply_text("Empty message. Please type your announcement.")
        return ConversationHandler.END

    if message_text.lower() in ("/cancel", "cancel"):
        await update.effective_message.reply_text("Broadcast cancelled.")
        await admin_command(update, context)
        return ConversationHandler.END

    await _execute_broadcast(message_text, update, context)
    return ConversationHandler.END


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel broadcast."""
    query = update.callback_query
    if query:
        await query.answer()
    if update.effective_message:
        await update.effective_message.reply_text("Broadcast cancelled.")
    await admin_command(update, context)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#  Back to admin
# ═══════════════════════════════════════════════════════════════

async def admin_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Navigate back to admin dashboard."""
    await admin_command(update, context)


# ═══════════════════════════════════════════════════════════════
#  Referral Program Management (Admin)
# ═══════════════════════════════════════════════════════════════

async def admin_referrals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the referral program admin panel."""
    query = update.callback_query
    if query:
        await query.answer()

    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    from bot.services.referral import get_top_referrers, get_referral_settings, DEFAULT_COMMISSION_RATE

    # Get current settings
    bot_data = context.bot_data or {}
    ref_settings = await get_referral_settings(bot_data)
    enabled = ref_settings.get("enabled", True)
    rate = ref_settings.get("commission_rate", DEFAULT_COMMISSION_RATE)

    # Get total stats
    async with async_session() as session:
        from bot.models.referral import ReferralEarning
        total_commissions_paid = await session.scalar(
            select(func.sum(ReferralEarning.amount))
        ) or 0
        total_commission_count = await session.scalar(
            select(func.count(ReferralEarning.id))
        ) or 0
        total_referred_users = await session.scalar(
            select(func.count(User.id)).where(User.referred_by.isnot(None))
        ) or 0

    # Top referrers
    top_referrers = await get_top_referrers(limit=5)
    top_lines = []
    for i, r in enumerate(top_referrers, 1):
        name = r["first_name"] or r["username"] or f"ID:{r['telegram_id']}"
        top_lines.append(
            f"{i}. *{name}* — {r['referral_count']} refs • ₦{r['total_earnings']:,.2f}"
        )
    top_text = "\n".join(top_lines) if top_lines else "_No referrals yet_"

    status_emoji = "✅" if enabled else "❌"
    text = (
        "*Referral Program Management*\n"
        "\n"
        f"• Status: {status_emoji} *{'Enabled' if enabled else 'Disabled'}*\n"
        "• Referral Bonus: *₦500 per purchase*\n"
        f"• Total Referred Users: `{total_referred_users}`\n"
        f"• Total Commissions Paid: `{total_commission_count}`\n"
        f"• Total Amount Paid: *`₦{total_commissions_paid:,.2f}`*\n"
        "\n"
        "*🏆 Top Referrers:*\n"
        f"{top_text}\n"
        ""
    )

    toggle_text = "❌ Disable Referrals" if enabled else "✅ Enable Referrals"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(toggle_text, callback_data="admin_ref_toggle"),
        ],
        [
            InlineKeyboardButton("Fixed ₦500", callback_data="admin_ref_rate:0"),
        ],
        [
            InlineKeyboardButton("« Back to Admin", callback_data="admin_back"),
        ],
    ])

    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def admin_ref_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle referral program on/off."""
    query = update.callback_query
    if not query or not update.effective_user or not _is_admin(update.effective_user.id):
        return

    from bot.services.referral import get_referral_settings, save_referral_settings, DEFAULT_COMMISSION_RATE

    bot_data = context.bot_data or {}
    ref_settings = await get_referral_settings(bot_data)
    ref_settings["enabled"] = not ref_settings.get("enabled", True)
    await save_referral_settings(bot_data, ref_settings)

    status = "enabled ✅" if ref_settings["enabled"] else "disabled ❌"
    await query.answer(f"Referral program {status}", show_alert=True)
    await admin_referrals_callback(update, context)


async def admin_ref_rate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change referral commission rate."""
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not _is_admin(update.effective_user.id):
        return

    from bot.services.referral import get_referral_settings, save_referral_settings, DEFAULT_COMMISSION_RATE

    rate = float(query.data.split(":")[1])
    bot_data = context.bot_data or {}
    ref_settings = await get_referral_settings(bot_data)
    ref_settings["commission_rate"] = rate
    await save_referral_settings(bot_data, ref_settings)

    await query.answer("Referral bonus is fixed at ₦500 per purchase", show_alert=True)
    await admin_referrals_callback(update, context)


# ═══════════════════════════════════════════════════════════════
#  Forex / Exchange Rates Management
# ═══════════════════════════════════════════════════════════════

async def admin_rates_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the Forex / Exchange Rates management console."""
    query = update.callback_query
    if query:
        await query.answer()

    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    from bot.services.forex import get_forex_summary
    forex = await get_forex_summary()
    active_ngn = forex["active_rate_ngn"]
    is_auto = forex["is_auto"]
    buffer_pct = forex["buffer_percent"]
    source = forex["last_sync_source"]
    last_sync = forex["last_sync_at"]
    sync_str = last_sync.strftime("%d %b %Y, %H:%M UTC") if last_sync else "Pending first sync"

    rates_map = forex.get("tracked_rates", {})
    ghs_rate = rates_map.get("GHS", 11.64)
    kes_rate = rates_map.get("KES", 129.8)
    zar_rate = rates_map.get("ZAR", 16.6)

    mode_status = "🟢 *Automatic (Live Daily Sync)*" if is_auto else "🟡 *Manual Override*"

    text = (
        "*💱 Forex & Currency Exchange Rates*\n"
        "\n"
        f"• Status: {mode_status}\n"
        f"• Active USD Rate: *`1 USD = ₦{active_ngn:,.2f}`*\n"
        f"• Rate Source: `{source}`\n"
        f"• Last Synchronized: `{sync_str}`\n"
        f"• FX Safety Buffer: *`+{buffer_pct:.1f}%`*\n"
        "\n"
        "*African Regional Currencies (per USD):*\n"
        f"• 🇬🇭 Ghana (GHS): `{ghs_rate:.2f} GH₵`\n"
        f"• 🇰🇪 Kenya (KES): `{kes_rate:.2f} KSh`\n"
        f"• 🇿🇦 South Africa (ZAR): `{zar_rate:.2f} R`\n"
        "\n"
        "_Rates update automatically every day from official institutional\n"
        "forex feeds (ExchangeRate-API) with multi-provider failover._"
    )

    mode_btn_text = "Switch to Manual Mode" if is_auto else "Switch to Auto Live Sync"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh Rates Now", callback_data="admin_rates_refresh", api_kwargs={"style": "success"}),
        ],
        [
            InlineKeyboardButton(mode_btn_text, callback_data="admin_rates_toggle_mode"),
        ],
        [
            InlineKeyboardButton("Buffer: 0%", callback_data="admin_rates_buffer:0"),
            InlineKeyboardButton("Buffer: +2%", callback_data="admin_rates_buffer:2"),
            InlineKeyboardButton("Buffer: +5%", callback_data="admin_rates_buffer:5"),
        ],
        [
            InlineKeyboardButton("« Back to Admin", callback_data="admin_back"),
        ],
    ])

    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def admin_rates_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle on-demand manual rate refresh."""
    query = update.callback_query
    if not query or not update.effective_user or not _is_admin(update.effective_user.id):
        return

    from bot.services.forex import sync_forex_rates
    result = await sync_forex_rates(force=True)
    if result.get("success"):
        rate = result.get("rate_ngn", 0)
        source = result.get("source", "Live feed")
        await query.answer(f"✓ Rates refreshed!\n1 USD = ₦{rate:,.2f} ({source})", show_alert=True)
    else:
        err = result.get("error", "Unknown error")
        await query.answer(f"⚠️ Could not refresh live rates: {err}", show_alert=True)

    await admin_rates_callback(update, context)


async def admin_rates_toggle_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle between auto live rates and manual mode."""
    query = update.callback_query
    if not query or not update.effective_user or not _is_admin(update.effective_user.id):
        return

    from bot.services.forex import get_forex_summary, set_forex_mode
    summary = await get_forex_summary()
    new_mode = not summary["is_auto"]
    await set_forex_mode(is_auto=new_mode)
    label = "Automatic (Live Daily Sync)" if new_mode else "Manual Override"
    await query.answer(f"Forex mode switched to: {label}", show_alert=True)
    await admin_rates_callback(update, context)


async def admin_rates_buffer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the FX buffer percentage."""
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not _is_admin(update.effective_user.id):
        return

    from bot.services.forex import set_fx_buffer
    buf = float(query.data.split(":")[1])
    await set_fx_buffer(buf)
    await query.answer(f"FX Safety Buffer set to +{buf}%", show_alert=True)
    await admin_rates_callback(update, context)


# ═══════════════════════════════════════════════════════════════
#  Register
# ═══════════════════════════════════════════════════════════════

async def make_reseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_admin(update.effective_user.id) or not context.args:
        await update.effective_message.reply_text("Usage: /makereseller <telegram_id>")
        return
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == int(context.args[0])))
        if not user:
            await update.effective_message.reply_text("User not found.")
            return
        user.is_reseller = True
        await session.commit()
    await update.effective_message.reply_text("Reseller account enabled.")


async def remove_reseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_admin(update.effective_user.id) or not context.args:
        await update.effective_message.reply_text("Usage: /removereseller <telegram_id>")
        return
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == int(context.args[0])))
        if user:
            user.is_reseller = False
            await session.commit()
    await update.effective_message.reply_text("Reseller account disabled.")


async def make_super_reseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_admin(update.effective_user.id) or not context.args:
        await update.effective_message.reply_text("Usage: /makesuperreseller <telegram_id>")
        return
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == int(context.args[0])))
        if not user:
            await update.effective_message.reply_text("User not found.")
            return
        user.is_reseller = True
        user.is_super_reseller = True
        await session.commit()
    await update.effective_message.reply_text(
        "Super reseller enabled. This account will always buy fresh products from SubExpress."
    )


async def remove_super_reseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_admin(update.effective_user.id) or not context.args:
        await update.effective_message.reply_text("Usage: /removesuperreseller <telegram_id>")
        return
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == int(context.args[0])))
        if user:
            user.is_super_reseller = False
            await session.commit()
    await update.effective_message.reply_text("Super reseller status disabled.")


async def super_reseller_price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a super-reseller price: /superresellerprice <product_id> <price_ngn> [manual]."""
    if not update.effective_user or not _is_admin(update.effective_user.id) or len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /superresellerprice <product_id> <price_ngn> [manual]"
        )
        return
    from bot.models.super_reseller import SuperResellerPrice
    try:
        product_id = int(context.args[0])
        price = Decimal(context.args[1].replace(",", "").replace("₦", ""))
        if price <= 0:
            raise ValueError
    except (ValueError, ArithmeticError):
        await update.effective_message.reply_text("Enter a valid product ID and positive NGN price.")
        return

    source = context.args[2].lower() if len(context.args) > 2 else "subexpress"
    product_name = f"Product {product_id}"
    if source == "manual":
        from bot.models.manual_product import ManualProduct
        async with async_session() as session:
            product = await session.get(ManualProduct, product_id)
            if product:
                product_name = product.name
    else:
        from bot.services.subexpress import subexpress
        try:
            products = await subexpress.list_products()
            product = next((item for item in products if item.id == product_id), None)
            if product:
                product_name = product.name
        except Exception:
            logger.exception("Could not resolve product for super-reseller price")

    async with async_session() as session:
        row = await session.scalar(select(SuperResellerPrice).where(
            SuperResellerPrice.product_id == product_id,
            SuperResellerPrice.source == source,
        ))
        if row:
            row.price_ngn = price
            row.product_name = product_name
        else:
            session.add(SuperResellerPrice(
                product_id=product_id,
                source=source,
                product_name=product_name,
                price_ngn=price,
            ))
        await session.commit()
    await update.effective_message.reply_text(
        f"Super-reseller price saved for {product_name}: ₦{price:,.2f}."
    )


async def super_reseller_prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show tappable products for super-reseller price editing."""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    from bot.handlers.catalog import _get_products
    try:
        products = await _get_products()
    except Exception:
        products = []

    buttons = [
        [InlineKeyboardButton(
            product.name[:40],
            callback_data=f"super_reseller_select:{product.source}:{product.manual_product_id or product.id}",
        )]
        for product in products
    ]
    buttons.append([InlineKeyboardButton("Cancel", callback_data="super_reseller_price_cancel")])
    await update.effective_message.reply_text(
        "Select a product to set or reset its super-reseller price:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AWAITING_RESELLER_PRICE


async def super_reseller_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    await query.answer()
    _, source, product_id_text = query.data.split(":", 2)
    product_id = int(product_id_text)
    context.user_data["super_reseller_product_id"] = product_id
    context.user_data["super_reseller_product_source"] = source
    if query.message:
        await query.message.edit_text(
            f"Super-reseller price for product #{product_id}\n\n"
            "Send the new NGN price, or tap Reset to remove the custom price.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "Reset price",
                    callback_data=f"super_reseller_reset:{source}:{product_id}",
                )],
                [InlineKeyboardButton(
                    "Cancel",
                    callback_data="super_reseller_price_cancel",
                )],
            ]),
        )
    return AWAITING_RESELLER_PRICE


async def super_reseller_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message or not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        price = Decimal((update.effective_message.text or "").replace(",", "").replace("₦", ""))
        if price <= 0:
            raise ValueError
    except (ValueError, ArithmeticError):
        await update.effective_message.reply_text("Enter a valid positive NGN price, or tap Reset.")
        return AWAITING_RESELLER_PRICE

    product_id = context.user_data.get("super_reseller_product_id")
    source = context.user_data.get("super_reseller_product_source", "subexpress")
    if not product_id:
        return ConversationHandler.END
    from bot.models.super_reseller import SuperResellerPrice
    product_name = f"Product {product_id}"
    from bot.handlers.catalog import _get_products
    products = await _get_products()
    product = next(
        (item for item in products if item.source == source and (item.manual_product_id or item.id) == product_id),
        None,
    )
    if product:
        product_name = product.name
    async with async_session() as session:
        row = await session.scalar(select(SuperResellerPrice).where(
            SuperResellerPrice.product_id == product_id,
            SuperResellerPrice.source == source,
        ))
        if row:
            row.price_ngn = price
            row.product_name = product_name
        else:
            session.add(SuperResellerPrice(
                product_id=product_id,
                source=source,
                product_name=product_name,
                price_ngn=price,
            ))
        await session.commit()
    context.user_data.pop("super_reseller_product_id", None)
    context.user_data.pop("super_reseller_product_source", None)
    await update.effective_message.reply_text(
        f"Super-reseller price saved: {product_name} — ₦{price:,.2f}"
    )
    return ConversationHandler.END


async def super_reseller_reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    await query.answer()
    _, source, product_id_text = query.data.split(":", 2)
    product_id = int(product_id_text)
    from bot.models.super_reseller import SuperResellerPrice
    async with async_session() as session:
        row = await session.scalar(select(SuperResellerPrice).where(
            SuperResellerPrice.product_id == product_id,
            SuperResellerPrice.source == source,
        ))
        if row:
            await session.delete(row)
            await session.commit()
    context.user_data.pop("super_reseller_product_id", None)
    context.user_data.pop("super_reseller_product_source", None)
    if query.message:
        await query.message.edit_text("Super-reseller price reset to the normal product price.")
    return ConversationHandler.END


async def super_reseller_price_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("super_reseller_product_id", None)
    context.user_data.pop("super_reseller_product_source", None)
    if update.callback_query:
        await update.callback_query.answer()
        if update.callback_query.message:
            await update.callback_query.message.edit_text("Super-reseller price update cancelled.")
    elif update.effective_message:
        await update.effective_message.reply_text("Super-reseller price update cancelled.")
    return ConversationHandler.END


async def reseller_price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a price: /resellerprice <product_id> <price_ngn> [manual]."""
    if not update.effective_user or not _is_admin(update.effective_user.id) or len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /resellerprice <product_id> <price_ngn> [manual]")
        return
    from bot.models.reseller import ResellerPrice
    product_id = int(context.args[0])
    price = Decimal(context.args[1].replace(",", "").replace("₦", ""))
    source = context.args[2].lower() if len(context.args) > 2 else "subexpress"
    product_name = f"Product {product_id}"
    if source == "manual":
        from bot.models.manual_product import ManualProduct
        async with async_session() as session:
            product = await session.get(ManualProduct, product_id)
            if product:
                product_name = product.name
    else:
        from bot.services.subexpress import subexpress
        try:
            products = await subexpress.list_products()
            product = next((item for item in products if item.id == product_id), None)
            if product:
                product_name = product.name
        except Exception:
            pass
    async with async_session() as session:
        row = await session.scalar(select(ResellerPrice).where(
            ResellerPrice.product_id == product_id, ResellerPrice.source == source
        ))
        if row:
            row.price_ngn = price
            row.product_name = product_name
        else:
            session.add(ResellerPrice(product_id=product_id, source=source, product_name=product_name, price_ngn=price))
        await session.commit()
    await update.effective_message.reply_text(f"Reseller price saved for {product_name}.")


async def reseller_products_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List current IDs: /resellerproducts."""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    from bot.services.subexpress import subexpress
    try:
        products = await subexpress.list_products()
    except Exception:
        products = []
    lines = [f"{p.id} — {p.name} — ${p.price_usd:,.2f}" for p in products]
    from bot.models.manual_product import ManualProduct
    async with async_session() as session:
        result = await session.execute(select(ManualProduct).order_by(ManualProduct.id))
        manuals = result.scalars().all()
    lines.extend(f"manual {p.id} — {p.name} — ₦{p.price_ngn:,.2f}" for p in manuals)
    await update.effective_message.reply_text(
        "Current product IDs:\n\n" + ("\n".join(lines) if lines else "No products available.")
    )


async def reseller_prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show tappable products for reseller price editing."""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    from bot.handlers.catalog import _get_products
    try:
        products = await _get_products()
    except Exception:
        products = []

    buttons = []
    for product in products:
        source = product.source
        product_id = product.manual_product_id or product.id
        label = product.name[:40]
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"reseller_select:{source}:{product_id}",
            )
        ])

    buttons.append([InlineKeyboardButton("Cancel", callback_data="reseller_price_cancel")])
    message = update.effective_message
    if message:
        await message.reply_text(
            "Select a product to set its reseller price:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    return AWAITING_RESELLER_PRICE


async def reseller_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    await query.answer()
    _, source, product_id_text = query.data.split(":", 2)
    product_id = int(product_id_text)
    context.user_data["reseller_product_id"] = product_id
    context.user_data["reseller_product_source"] = source
    if query.message:
        await query.message.edit_text(
            f"Type the reseller price in NGN for product #{product_id}.\n\n"
            "Example: `7000`\n\nSend /cancel to stop.",
            parse_mode="Markdown",
        )
    return AWAITING_RESELLER_PRICE


async def reseller_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message or not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        price = Decimal((update.effective_message.text or "").replace(",", "").replace("₦", ""))
        if price <= 0:
            raise ValueError
    except (ValueError, ArithmeticError):
        await update.effective_message.reply_text("Enter a valid positive NGN price, or send /cancel.")
        return AWAITING_RESELLER_PRICE

    product_id = context.user_data.get("reseller_product_id")
    source = context.user_data.get("reseller_product_source", "subexpress")
    if not product_id:
        return ConversationHandler.END
    from bot.models.reseller import ResellerPrice
    product_name = f"Product {product_id}"
    from bot.handlers.catalog import _get_products
    products = await _get_products()
    product = next((item for item in products if item.source == source and (item.manual_product_id or item.id) == product_id), None)
    if product:
        product_name = product.name
    async with async_session() as session:
        row = await session.scalar(select(ResellerPrice).where(
            ResellerPrice.product_id == product_id,
            ResellerPrice.source == source,
        ))
        if row:
            row.price_ngn = price
            row.product_name = product_name
        else:
            session.add(ResellerPrice(
                product_id=product_id,
                source=source,
                product_name=product_name,
                price_ngn=price,
            ))
        await session.commit()
    context.user_data.pop("reseller_product_id", None)
    context.user_data.pop("reseller_product_source", None)
    await update.effective_message.reply_text(
        f"Reseller price saved: {product_name} — ₦{price:,.2f}"
    )
    return ConversationHandler.END


async def reseller_price_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reseller_product_id", None)
    context.user_data.pop("reseller_product_source", None)
    if update.callback_query:
        await update.callback_query.answer()
        if update.callback_query.message:
            await update.callback_query.message.edit_text("Reseller price update cancelled.")
    elif update.effective_message:
        await update.effective_message.reply_text("Reseller price update cancelled.")
    return ConversationHandler.END


async def reuse_products_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show live products for shared-account reuse configuration."""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    from bot.handlers.catalog import _get_products
    try:
        products = await _get_products()
    except Exception:
        products = []
    buttons = []
    for product in products:
        buttons.append([InlineKeyboardButton(
            product.name[:40],
            callback_data=f"reuse_select:{product.source}:{product.manual_product_id or product.id}",
        )])
    buttons.append([InlineKeyboardButton("Cancel", callback_data="reuse_cancel")])
    await update.effective_message.reply_text(
        "Select a product to configure shared-account reuse:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AWAITING_REUSE_LIMIT


async def admin_reuse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open the shared-account reuse product picker from the dashboard."""
    query = update.callback_query
    if query:
        await query.answer()
    return await reuse_products_command(update, context)


async def reuse_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    await query.answer()
    _, source, product_id_text = query.data.split(":", 2)
    product_id = int(product_id_text)
    from bot.handlers.catalog import _get_products
    products = await _get_products()
    product = next((item for item in products if item.source == source and (item.manual_product_id or item.id) == product_id), None)
    if not product:
        await query.message.edit_text("Product is no longer available.")
        return ConversationHandler.END
    from bot.services.shared_inventory import get_reuse_config
    config = await get_reuse_config(product_id, source)
    current = f"currently enabled for {config.max_reuses} reuse(s)" if config and config.enabled else "currently disabled"
    context.user_data["reuse_product_id"] = product_id
    context.user_data["reuse_product_source"] = source
    context.user_data["reuse_product_name"] = product.name
    await query.message.edit_text(
        f"*{product.name}* is {current}.\n\n"
        "Type the maximum number of buyers per fetched login.\n"
        "Use `0` to disable reuse.\n\nExample: `3`",
        parse_mode="Markdown",
    )
    return AWAITING_REUSE_LIMIT


async def reuse_limit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message or not update.effective_user or not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        max_reuses = int((update.effective_message.text or "").strip())
        if max_reuses < 0 or max_reuses > 100:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Enter a whole number from 0 to 100, or send /cancel.")
        return AWAITING_REUSE_LIMIT
    product_id = context.user_data.get("reuse_product_id")
    source = context.user_data.get("reuse_product_source", "subexpress")
    product_name = context.user_data.get("reuse_product_name", f"Product {product_id}")
    from bot.services.shared_inventory import save_reuse_config
    config = await save_reuse_config(product_id, source, product_name, max_reuses)
    context.user_data.pop("reuse_product_id", None)
    context.user_data.pop("reuse_product_source", None)
    context.user_data.pop("reuse_product_name", None)
    status = "enabled" if config.enabled else "disabled"
    await update.effective_message.reply_text(
        f"Shared reuse {status} for {product_name}. Maximum buyers per login: {max_reuses}."
    )
    return ConversationHandler.END


async def reuse_from_order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Seed reuse from an existing fulfilled order: /reusefromorder <order_id>."""
    if not update.effective_user or not _is_admin(update.effective_user.id) or not context.args:
        await update.effective_message.reply_text("Usage: /reusefromorder <fulfilled_order_id>")
        return
    try:
        order_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Order ID must be a number.")
        return

    async with async_session() as session:
        order = await session.get(Order, order_id)
        if not order or order.status != OrderStatus.FULFILLED.value:
            await update.effective_message.reply_text("That order was not found or is not fulfilled.")
            return
        credentials = order.keys
        product_id = order.product_id
        source = order.source or "subexpress"
        product_name = order.product_name

    if not credentials:
        await update.effective_message.reply_text("That order has no saved login details to reuse.")
        return

    from bot.services.shared_inventory import get_reuse_config, store_provider_account
    config = await get_reuse_config(product_id, source)
    if not config or not config.enabled or config.max_reuses <= 0:
        await update.effective_message.reply_text(
            "Reuse is not enabled for this product. Configure it first from the Shared Account Reuse menu."
        )
        return
    account = await store_provider_account(product_id, source, product_name, credentials)
    if not account:
        await update.effective_message.reply_text("Could not create the reusable account.")
        return
    await update.effective_message.reply_text(
        f"Reusable account seeded from order #{order_id}. "
        f"It has {account.assigned_count}/{account.max_reuses} assigned use(s)."
    )


async def reuse_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ("reuse_product_id", "reuse_product_source", "reuse_product_name"):
        context.user_data.pop(key, None)
    if update.callback_query:
        await update.callback_query.answer()
        if update.callback_query.message:
            await update.callback_query.message.edit_text("Shared reuse configuration cancelled.")
    elif update.effective_message:
        await update.effective_message.reply_text("Shared reuse configuration cancelled.")
    return ConversationHandler.END


async def admin_resellers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not _is_admin(update.effective_user.id):
        return
    await query.answer()
    async with async_session() as session:
        result = await session.execute(select(User).where(User.is_reseller == True))  # noqa: E712
        users = result.scalars().all()
    lines = [f"• {u.username or u.first_name or u.telegram_id} (`{u.telegram_id}`)" for u in users]
    text = "*Reseller Accounts*\n\n"
    text += "\n".join(lines) if lines else "_No reseller accounts yet._"
    text += (
        "\n\nUse /makereseller, /removereseller, /resellerprice, "
        "/makesuperreseller, /removesuperreseller, and /superresellerprice."
    )
    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Back to Admin", callback_data="admin_back")]
    ]))


async def admin_manuals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not _is_admin(update.effective_user.id):
        return
    await query.answer()
    await query.message.edit_text(
        "Manual Products\n\n"
        "Use /addmanual, /editmanual, /deletemanual, and /manuals.\n"
        "Fulfill paid orders with /fulfill <order_id> <delivery text>.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Admin", callback_data="admin_back")]]),
    )

def register(app) -> None:
    """Register all admin handlers."""

    # Price editing conversation
    price_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(setprice_select_callback, pattern=r"^setprice_select:\d+$"),
            CallbackQueryHandler(setprice_preset_callback, pattern=r"^price_preset:\d+:"),
        ],
        states={
            AWAITING_PRICE: [
                CallbackQueryHandler(setprice_preset_callback, pattern=r"^price_preset:\d+:"),
                CallbackQueryHandler(setprice_cancel, pattern=r"^admin_prices$"),
                CallbackQueryHandler(setprice_cancel, pattern=r"^admin_back$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setprice_receive_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", setprice_cancel),
            CallbackQueryHandler(setprice_cancel, pattern=r"^admin_price_cancel$"),
            CallbackQueryHandler(setprice_cancel, pattern=r"^admin_prices$"),
            CallbackQueryHandler(setprice_cancel, pattern=r"^admin_back$"),
        ],
        per_message=False,
    )
    app.add_handler(price_conv, group=-1)

    # Broadcast conversation
    broadcast_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(broadcast_start, pattern=r"^admin_broadcast$"),
            CommandHandler("broadcast", broadcast_start),
        ],
        states={
            AWAITING_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", broadcast_cancel),
            CallbackQueryHandler(broadcast_cancel, pattern=r"^admin_broadcast_cancel$"),
        ],
        per_message=False,
    )
    app.add_handler(broadcast_conv)

    reseller_price_conv = ConversationHandler(
        entry_points=[CommandHandler("resellerprices", reseller_prices_command)],
        states={
            AWAITING_RESELLER_PRICE: [
                CallbackQueryHandler(reseller_select_callback, pattern=r"^reseller_select:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reseller_price_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", reseller_price_cancel),
            CallbackQueryHandler(reseller_price_cancel, pattern=r"^reseller_price_cancel$"),
        ],
        per_message=False,
    )
    app.add_handler(reseller_price_conv, group=-1)

    super_reseller_price_conv = ConversationHandler(
        entry_points=[CommandHandler("superresellerprices", super_reseller_prices_command)],
        states={
            AWAITING_RESELLER_PRICE: [
                CallbackQueryHandler(
                    super_reseller_select_callback,
                    pattern=r"^super_reseller_select:",
                ),
                CallbackQueryHandler(
                    super_reseller_reset_callback,
                    pattern=r"^super_reseller_reset:",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, super_reseller_price_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", super_reseller_price_cancel),
            CallbackQueryHandler(
                super_reseller_price_cancel,
                pattern=r"^super_reseller_price_cancel$",
            ),
        ],
        per_message=False,
    )
    app.add_handler(super_reseller_price_conv, group=-1)

    reuse_conv = ConversationHandler(
        entry_points=[
            CommandHandler("reuseproducts", reuse_products_command),
            CallbackQueryHandler(admin_reuse_callback, pattern=r"^admin_reuse$"),
        ],
        states={
            AWAITING_REUSE_LIMIT: [
                CallbackQueryHandler(reuse_select_callback, pattern=r"^reuse_select:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reuse_limit_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", reuse_cancel),
            CallbackQueryHandler(reuse_cancel, pattern=r"^reuse_cancel$"),
        ],
        per_message=False,
    )
    app.add_handler(reuse_conv, group=-1)

    # Admin commands
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("setprice", setprice_command))
    app.add_handler(CommandHandler("resetprice", resetprice_command))
    app.add_handler(CommandHandler("fund", fund_command))
    app.add_handler(CommandHandler("debit", debit_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("makereseller", make_reseller_command))
    app.add_handler(CommandHandler("removereseller", remove_reseller_command))
    app.add_handler(CommandHandler("makesuperreseller", make_super_reseller_command))
    app.add_handler(CommandHandler("removesuperreseller", remove_super_reseller_command))
    app.add_handler(CommandHandler("resellerprice", reseller_price_command))
    app.add_handler(CommandHandler("superresellerprice", super_reseller_price_command))
    app.add_handler(CommandHandler("superresellerprices", super_reseller_prices_command))
    app.add_handler(CommandHandler("resellerproducts", reseller_products_command))
    app.add_handler(CommandHandler("reusefromorder", reuse_from_order_command))

    # Admin callbacks
    app.add_handler(CallbackQueryHandler(prices_callback, pattern=r"^admin_prices$"))
    app.add_handler(CallbackQueryHandler(prices_callback, pattern=r"^admin_price_page:\d+$"))
    app.add_handler(CallbackQueryHandler(setprice_preset_callback, pattern=r"^price_preset:\d+:"))
    app.add_handler(CallbackQueryHandler(users_callback, pattern=r"^admin_users$"))
    app.add_handler(CallbackQueryHandler(orders_callback, pattern=r"^admin_orders$"))
    app.add_handler(CallbackQueryHandler(stats_callback, pattern=r"^admin_stats$"))
    app.add_handler(CallbackQueryHandler(transactions_callback, pattern=r"^admin_transactions(?::\d+)?$"))
    app.add_handler(CallbackQueryHandler(admin_referrals_callback, pattern=r"^admin_referrals$"))
    app.add_handler(CallbackQueryHandler(admin_resellers_callback, pattern=r"^admin_resellers$"))
    app.add_handler(CallbackQueryHandler(admin_manuals_callback, pattern=r"^admin_manuals$"))
    app.add_handler(CallbackQueryHandler(admin_ref_toggle_callback, pattern=r"^admin_ref_toggle$"))
    app.add_handler(CallbackQueryHandler(admin_ref_rate_callback, pattern=r"^admin_ref_rate:"))
    app.add_handler(CallbackQueryHandler(admin_rates_callback, pattern=r"^admin_rates$"))
    app.add_handler(CallbackQueryHandler(admin_rates_refresh_callback, pattern=r"^admin_rates_refresh$"))
    app.add_handler(CallbackQueryHandler(admin_rates_toggle_mode_callback, pattern=r"^admin_rates_toggle_mode$"))
    app.add_handler(CallbackQueryHandler(admin_rates_buffer_callback, pattern=r"^admin_rates_buffer:"))
    app.add_handler(CallbackQueryHandler(admin_back_callback, pattern=r"^admin_back$"))
