"""
DRSub – Purchase handler.

Handles the full buy flow: product selection → quantity → confirmation
→ wallet debit → SubExpress order → key delivery.
Also handles the 'My Orders' view.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select, update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.helpers import escape_markdown
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
from bot.services.subexpress import subexpress, SubExpressError

logger = logging.getLogger(__name__)

# Conversation states
AWAITING_QUANTITY = 0


def _markdown_text(value: object) -> str:
    """Escape dynamic values before inserting them into Markdown messages."""
    return escape_markdown(str(value), version=1)


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

async def _get_user_and_wallet(telegram_id: int) -> tuple[User | None, Wallet | None]:
    """Fetch a user and their wallet."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return None, None

        wallet_result = await session.execute(
            select(Wallet).where(Wallet.user_id == user.id)
        )
        wallet = wallet_result.scalar_one_or_none()
        return user, wallet


async def _reseller_id(telegram_id: int) -> int | None:
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        return user.id if user and user.is_reseller else None


async def _is_super_reseller(telegram_id: int) -> bool:
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        return bool(user and user.is_super_reseller)


async def _refresh_purchase_product(telegram_id: int, product_id: int, quantity: int) -> dict | None:
    """Load the authoritative price for this buyer immediately before payment."""
    from bot.handlers.catalog import _get_products

    products = await _get_products(reseller_user_id=await _reseller_id(telegram_id))
    product = next((item for item in products if item.id == product_id), None)
    if not product or not product.in_stock:
        return None
    if quantity < product.min_qty or quantity > product.max_qty:
        return None
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "price_usd": product.price_usd,
        "price_ngn": product.price_ngn,
        "min_qty": product.min_qty,
        "max_qty": product.max_qty,
        "source": product.source,
        "manual_product_id": product.manual_product_id,
        "quantity": quantity,
        "total_ngn": float(Decimal(str(product.price_ngn)) * quantity),
        "total_usd": float(Decimal(str(product.price_usd)) * quantity),
    }


async def _finish_shared_wallet_order(
    order_id: int,
    account_id: int,
    credentials: list,
    product_name: str,
    quantity: int,
    total_ngn: Decimal,
    query,
    context,
    user_id: int,
) -> None:
    async with async_session() as session:
        order = await session.get(Order, order_id)
        order.shared_account_id = account_id
        order.keys = credentials
        order.status = OrderStatus.FULFILLED.value
        await session.commit()
    keys_display = "\n".join(f"`{key}`" for key in credentials)
    if query.message:
        await query.message.edit_text(
            "✅ *Order Fulfilled*\n\n"
            "📦 *Order details*\n"
            f"Product: *{product_name}*\n"
            f"Quantity: `{quantity}`\n"
            f"Paid: *`₦{total_ngn:,.2f}`*\n"
            f"Order ID: `#{order_id}`\n\n"
            "🔐 *Login details*\n"
            f"{keys_display}\n\n"
            "Please save these details.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Shop Subscriptions", callback_data="catalog", api_kwargs={"style": "success"})],
                [InlineKeyboardButton("Order History", callback_data="my_orders", api_kwargs={"style": "primary"})],
            ]),
        )
    try:
        from bot.services.referral import process_referral_commission, get_referral_settings
        from bot.services.payment_poller import _bot_app
        ref_settings = await get_referral_settings(context.bot_data or {})
        if ref_settings.get("enabled", True):
            await process_referral_commission(
                order=order,
                buyer_user_id=user_id,
                order_amount_ngn=total_ngn,
                product_name=product_name,
                bot_app=_bot_app,
            )
    except Exception as exc:
        logger.error("Shared-account referral commission error: %s", exc)


# ═══════════════════════════════════════════════════════════════
#  Buy flow
# ═══════════════════════════════════════════════════════════════

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Handle 'buy:<product_id>' — start the purchase flow."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    logger.info(
        "Pay Now callback received: tg_user=%s data=%s",
        update.effective_user.id if update.effective_user else "unknown",
        query.data,
    )
    await query.answer()

    # Parse product ID
    parts = query.data.split(":")
    if len(parts) < 2:
        return ConversationHandler.END
    try:
        product_id = int(parts[1])
    except ValueError:
        logger.error("Pay Now callback has invalid product ID: data=%s", query.data)
        if query.message:
            await query.message.edit_text("Invalid product selection. Please open the catalog again.")
        return ConversationHandler.END

    # Fetch product info from cache
    from bot.handlers.catalog import _get_products
    try:
        products = await _get_products(reseller_user_id=await _reseller_id(update.effective_user.id))
    except Exception:
        logger.exception("Pay Now product lookup failed: tg_user=%s product_id=%s", update.effective_user.id, product_id)
        if query.message:
            await query.message.edit_text("⚠️ Could not load product info. Try again.")
        return ConversationHandler.END

    product = next((p for p in products if p.id == product_id), None)
    if not product:
        if query.message:
            await query.message.edit_text("❌ Product not found.")
        return ConversationHandler.END

    if not product.in_stock:
        if query.message:
            await query.message.edit_text(
                "❌ This product is currently out of stock.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Browse Products", callback_data="catalog")],
                ]),
            )
        return ConversationHandler.END

    # Store product info in user context for the conversation
    context.user_data["buy_product"] = {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "price_usd": product.price_usd,
        "price_ngn": product.price_ngn,
        "min_qty": product.min_qty,
        "max_qty": product.max_qty,
        "source": product.source,
        "manual_product_id": product.manual_product_id,
    }

    # If max quantity is 1, skip quantity prompt → go straight to confirmation
    if product.max_qty <= 1:
        context.user_data["buy_product"]["quantity"] = 1
        try:
            await _show_confirmation(query.message, context)
        except Exception:
            logger.exception(
                "Pay Now confirmation failed: tg_user=%s product_id=%s",
                update.effective_user.id if update.effective_user else "unknown",
                product_id,
            )
            if query.message:
                await query.message.edit_text(
                    "⚠️ Could not open checkout. Please try the product again."
                )
        return ConversationHandler.END

    # Ask for quantity with quick-select buttons
    qty_buttons = []
    row = []
    for q in range(product.min_qty, min(product.max_qty + 1, product.min_qty + 6)):
        row.append(InlineKeyboardButton(f"{q}", callback_data=f"qty:{product.id}:{q}"))
        if len(row) == 3:
            qty_buttons.append(row)
            row = []
    if row:
        qty_buttons.append(row)
    qty_buttons.append([InlineKeyboardButton("Cancel", callback_data="catalog", api_kwargs={"style": "danger"})])

    text = (
        f"🛒 *Order: {_markdown_text(product.name)}*\n\n"
        "💰 *Pricing*\n"
        f"Unit price: *`{product.price_ngn_display}`*\n"
        f"Quantity range: `{product.min_qty}` to `{product.max_qty}`\n\n"
        "Select a quantity below or reply with a number in chat:"
    )

    if query.message:
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(qty_buttons),
        )

    return AWAITING_QUANTITY


async def quantity_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the quantity input."""
    if not update.effective_message:
        return ConversationHandler.END

    product_data = context.user_data.get("buy_product")
    if not product_data:
        await update.effective_message.reply_text("Session expired. Please select a product again.")
        return ConversationHandler.END

    raw = (update.effective_message.text or "").strip()
    try:
        quantity = int(raw)
    except ValueError:
        await update.effective_message.reply_text(
            f"Please enter a valid number between {product_data['min_qty']} and {product_data['max_qty']}."
        )
        return AWAITING_QUANTITY

    if quantity < product_data["min_qty"] or quantity > product_data["max_qty"]:
        await update.effective_message.reply_text(
            f"Quantity must be between {product_data['min_qty']} and {product_data['max_qty']}."
        )
        return AWAITING_QUANTITY

    context.user_data["buy_product"]["quantity"] = quantity
    await _show_confirmation(update.effective_message, context, edit=False)
    return ConversationHandler.END


async def _show_confirmation(message, context: ContextTypes.DEFAULT_TYPE, edit: bool = True) -> None:
    """Show order confirmation with all available payment methods (Wallet, Bank Transfer, Card/USSD)."""
    product_data = context.user_data.get("buy_product", {})
    quantity = product_data.get("quantity", 1)
    price_per_unit = product_data.get("price_ngn", 0)
    total_ngn = Decimal(str(price_per_unit)) * quantity

    telegram_id = message.chat_id
    user, wallet = await _get_user_and_wallet(telegram_id)
    balance = wallet.balance if wallet else Decimal("0.00")
    sufficient = balance >= total_ngn

    context.user_data["buy_product"]["total_ngn"] = float(total_ngn)
    context.user_data["buy_product"]["total_usd"] = float(Decimal(str(product_data.get("price_usd", 0))) * quantity)

    from bot.services.currency import (
        get_user_currency,
        get_currency_info,
        format_price,
        format_dual_price,
        convert_ngn_to_local,
    )
    curr_code = await get_user_currency(telegram_id)
    curr_info = get_currency_info(curr_code)

    unit_display = format_dual_price(product_data.get("price_usd", 0), curr_code, price_per_unit)
    total_display = format_dual_price(product_data.get("price_usd", 0) * quantity, curr_code, float(total_ngn))
    btn_price = format_price(product_data.get("price_usd", 0) * quantity, curr_code, float(total_ngn))

    wallet_display = f"₦{balance:,.2f}"
    if curr_code != "NGN":
        _, local_bal_str = convert_ngn_to_local(float(balance), curr_code)
        wallet_display = f"{local_bal_str} (~₦{balance:,.2f})"

    status_tag = "[Sufficient]" if sufficient else "[Top-up Needed]"
    text = (
        f"🧾 *Order Checkout* · {curr_info.flag} {curr_info.code}\n\n"
        "📦 *Order details*\n"
        f"Product: *{_markdown_text(product_data['name'])}*\n"
        f"Quantity: `{quantity}`\n"
        f"Unit price: `{unit_display}`\n"
        f"Total payable: *`{total_display}`*\n\n"
        "👛 *Wallet*\n"
        f"Balance: `{wallet_display}` {status_tag}\n"
    )

    if not sufficient:
        deficit = total_ngn - balance
        deficit_display = f"₦{deficit:,.2f}"
        if curr_code != "NGN":
            _, local_def = convert_ngn_to_local(float(deficit), curr_code)
            deficit_display = f"{local_def} (~₦{deficit:,.2f})"
        text += f"Amount short: `{deficit_display}`\n"

    text += (
        "\n💳 *Payment options*\n"
        "Select your preferred payment method below:"
    )

    keyboard_rows = []

    # 1. Method 1: Pay via Bank Transfer (Paystack payment)
    keyboard_rows.append([
        InlineKeyboardButton(
            f"Pay with Bank Transfer ({btn_price})",
            callback_data=f"direct_pay:{product_data['id']}:{quantity}",
            api_kwargs={"style": "primary" if sufficient else "success"},
        )
    ])

    # 2. Method 2: Pay with Wallet
    if sufficient:
        keyboard_rows.append([
            InlineKeyboardButton(
                f"Pay with Wallet ({btn_price})",
                callback_data=f"confirm_buy:{product_data['id']}:{quantity}",
                api_kwargs={"style": "success"},
            )
        ])
    else:
        keyboard_rows.append([
            InlineKeyboardButton(
                f"Pay with Wallet (Top-up needed)",
                callback_data=f"wallet_insufficient:{product_data['id']}:{quantity}",
                api_kwargs={"style": "danger"},
            )
        ])

    # 3. Fund Wallet First
    keyboard_rows.append([
        InlineKeyboardButton(
            "Fund Wallet First (Deposit)",
            callback_data="deposit",
        )
    ])

    # 4. Navigation
    keyboard_rows.append([
        InlineKeyboardButton("Back to Catalog", callback_data="catalog"),
        InlineKeyboardButton("Main Menu", callback_data="start"),
    ])

    keyboard = InlineKeyboardMarkup(keyboard_rows)

    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def confirm_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'confirm_buy:<product_id>:<quantity>' — execute the purchase."""
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return
    logger.info(
        "Wallet Pay callback received: tg_user=%s data=%s",
        update.effective_user.id, query.data,
    )
    await query.answer("⏳ Processing your order…")

    # Parse callback
    parts = query.data.split(":")
    if len(parts) < 3:
        return
    product_id = int(parts[1])
    quantity = int(parts[2])

    product_data = context.user_data.get("buy_product", {})
    if not product_data or product_data.get("id") != product_id:
        if query.message:
            await query.message.edit_text("⚠️ Session expired. Please browse products again.")
        return

    telegram_id = update.effective_user.id
    refreshed_product = await _refresh_purchase_product(telegram_id, product_id, quantity)
    if not refreshed_product:
        if query.message:
            await query.message.edit_text("⚠️ This product is no longer available. Please browse the catalog again.")
        return
    product_data = refreshed_product
    context.user_data["buy_product"] = product_data
    total_ngn = Decimal(str(product_data.get("total_ngn", 0)))
    total_usd = Decimal(str(product_data.get("price_usd", 0))) * quantity

    async with async_session() as session:
        # Fetch user
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            if query.message:
                await query.message.edit_text("⚠️ User not found. Please /start.")
            return

        # Debit only if the balance is still sufficient. The conditional
        # update is atomic, preventing concurrent callback replays from
        # spending the same balance twice.
        debit_result = await session.execute(
            update(Wallet)
            .where(Wallet.user_id == user.id, Wallet.balance >= total_ngn)
            .values(balance=Wallet.balance - total_ngn)
        )
        if debit_result.rowcount != 1:
            if query.message:
                await query.message.edit_text(
                    "❌ Insufficient balance. Please deposit funds first.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Deposit", callback_data="deposit")],
                    ]),
                )
            return

        # ── Step 1: Debit wallet ─────────────────────────────
        # Create purchase transaction
        tx = Transaction(
            user_id=user.id,
            type=TransactionType.PURCHASE.value,
            amount=total_ngn,
            status=TransactionStatus.COMPLETED.value,
            description=f"Purchase: {product_data['name']} ×{quantity}",
        )
        session.add(tx)
        await session.flush()  # Get tx.id

        # Create order record
        order = Order(
            user_id=user.id,
            transaction_id=tx.id,
            product_id=product_id,
            product_name=product_data["name"],
            quantity=quantity,
            amount_ngn=total_ngn,
            amount_usd=total_usd,
            status=OrderStatus.PAID.value,
            source=product_data.get("source", "subexpress"),
            manual_product_id=product_data.get("manual_product_id"),
        )
        session.add(order)
        await session.flush()  # Get order.id

        await session.commit()
        logger.info(
            "Wallet order created: order_id=%s tg_user=%s product_id=%s amount_ngn=%.2f source=%s",
            order.id, telegram_id, product_id, float(total_ngn),
            product_data.get("source", "subexpress"),
        )

    # Manual products are paid immediately but fulfilled by an administrator.
    if product_data.get("source") == "manual":
        async with async_session() as session:
            order_result = await session.execute(select(Order).where(Order.id == order.id))
            db_order = order_result.scalar_one()
            db_order.status = OrderStatus.PENDING.value
            await session.commit()
        from bot.main import get_app
        app = get_app()
        if app:
            for admin_id in settings.admin_id_list:
                try:
                    await app.bot.send_message(
                        chat_id=admin_id,
                        text=(f"🛒 *Manual order awaiting fulfillment*\n\n"
                              f"Order: `#{order.id}`\nProduct: *{_markdown_text(product_data['name'])}*\n"
                              f"User: `{telegram_id}`\nAmount: ₦{total_ngn:,.2f}"),
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Could not alert admin for manual order")
        if query.message:
            await query.message.edit_text(
                f"✅ Payment received for *{_markdown_text(product_data['name'])}*.\n\n"
                "An administrator will fulfill this order manually and message you here.",
                parse_mode="Markdown",
            )
        context.user_data.pop("buy_product", None)
        return

    from bot.services.shared_inventory import claim_shared_account, account_credentials, store_provider_account
    super_reseller = await _is_super_reseller(telegram_id)
    shared_account = None if super_reseller else await claim_shared_account(
        product_id, product_data.get("source", "subexpress")
    )
    if shared_account:
        await _finish_shared_wallet_order(
            order.id,
            shared_account.id,
            account_credentials(shared_account),
            product_data["name"],
            quantity,
            total_ngn,
            query,
            context,
            user.id,
        )
        context.user_data.pop("buy_product", None)
        return

    # ── Step 2: Place SubExpress order ───────────────────────
    if query.message:
        await query.message.edit_text(
            f"⏳ *Processing order…*\n\n"
            f"🏷️ {product_data['name']} × {quantity}\n"
            f"💵 ₦{total_ngn:,.2f} debited from wallet",
            parse_mode="Markdown",
        )

    try:
        result = await subexpress.place_order(product_id, quantity)

        # ── Step 3: Deliver keys ─────────────────────────────
        async with async_session() as session:
            order_result = await session.execute(
                select(Order).where(Order.id == order.id)
            )
            db_order = order_result.scalar_one()
            db_order.subexpress_order_id = result.order_id
            db_order.keys = result.items
            db_order.status = OrderStatus.FULFILLED.value
            await session.commit()

        # If reuse is enabled, this provider response becomes the first slot
        # in a new shared account. The current buyer receives slot one.
        shared_account = None
        if not super_reseller:
            shared_account = await store_provider_account(
                product_id,
                product_data.get("source", "subexpress"),
                product_data["name"],
                result.items,
            )
        if shared_account:
            async with async_session() as session:
                db_order = await session.get(Order, order.id)
                db_order.shared_account_id = shared_account.id
                await session.commit()

        # Format keys for display
        keys_display = "\n".join(f"`{key}`" for key in result.items)

        success_text = (
            "✅ *Order Fulfilled*\n\n"
            "📦 *Order details*\n"
            f"Product: *{_markdown_text(product_data['name'])}*\n"
            f"Quantity: `{quantity}`\n"
            f"Paid: *`₦{total_ngn:,.2f}`*\n"
            f"Order ID: `#{order.id}`\n\n"
            "🔑 *Activation key"
            f"{'s' if quantity > 1 else ''}*\n"
            f"{keys_display}\n"
            "\nPlease copy and save your key. It is permanently saved in Order History."
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Shop Subscriptions", callback_data="catalog", api_kwargs={"style": "success"})],
            [InlineKeyboardButton("Order History", callback_data="my_orders", api_kwargs={"style": "primary"})],
            [InlineKeyboardButton("Main Menu", callback_data="start")],
        ])

        if query.message:
            await query.message.edit_text(
                success_text, parse_mode="Markdown", reply_markup=keyboard
            )

        logger.info(
            "Order fulfilled: order_id=%d user_tg=%d product=%s qty=%d",
            order.id, telegram_id, product_data["name"], quantity,
        )

        # Process referral commission
        try:
            from bot.services.referral import process_referral_commission, get_referral_settings
            from bot.services.payment_poller import _bot_app
            bot_data = context.bot_data or {}
            ref_settings = await get_referral_settings(bot_data)
            if ref_settings.get("enabled", True):
                await process_referral_commission(
                    order=order,
                    buyer_user_id=user.id,
                    order_amount_ngn=total_ngn,
                    product_name=product_data["name"],
                    bot_app=_bot_app,
                    commission_rate=ref_settings.get("commission_rate"),
                )
        except Exception as ref_exc:
            logger.error("Referral commission error: %s", ref_exc)

    except SubExpressError as exc:
        # ── Fulfillment failed → refund wallet ───────────────
        logger.error("SubExpress order failed: %s", exc)

        async with async_session() as session:
            # Update order status
            order_result = await session.execute(
                select(Order).where(Order.id == order.id)
            )
            db_order = order_result.scalar_one()
            db_order.status = OrderStatus.REFUNDED.value
            db_order.error_message = str(exc)

            # Refund wallet
            wallet_result = await session.execute(
                select(Wallet).where(Wallet.user_id == user.id)
            )
            db_wallet = wallet_result.scalar_one()
            db_wallet.balance += total_ngn

            # Create refund transaction
            refund_tx = Transaction(
                user_id=user.id,
                type=TransactionType.REFUND.value,
                amount=total_ngn,
                status=TransactionStatus.COMPLETED.value,
                description=f"Refund: {product_data['name']} (order failed)",
            )
            session.add(refund_tx)
            await session.commit()

        error_text = (
            "❌ *Order Failed*\n\n"
            f"{exc.user_message}\n\n"
            f"`₦{total_ngn:,.2f}` has been refunded to your wallet balance."
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Shop Subscriptions", callback_data="catalog", api_kwargs={"style": "primary"})],
            [InlineKeyboardButton("Main Menu", callback_data="start")],
        ])

        if query.message:
            await query.message.edit_text(
                error_text, parse_mode="Markdown", reply_markup=keyboard
            )

    # Clear session data
    context.user_data.pop("buy_product", None)


async def buy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the buy flow."""
    context.user_data.pop("buy_product", None)
    if update.effective_message:
        await update.effective_message.reply_text(
            "❌ Purchase cancelled.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Browse Products", callback_data="catalog")],
            ]),
        )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#  My Orders view
# ═══════════════════════════════════════════════════════════════

async def my_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's recent orders."""
    query = update.callback_query
    if query:
        await query.answer()

    tg_user = update.effective_user
    if not tg_user:
        return

    async with async_session() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == tg_user.id)
        )
        user = user_result.scalar_one_or_none()

        if not user:
            msg = query.message if query else update.effective_message
            if msg:
                text = "⚠️ Please send /start first."
                if query:
                    await msg.edit_text(text)
                else:
                    await msg.reply_text(text)
            return

        orders_result = await session.execute(
            select(Order)
            .where(Order.user_id == user.id)
            .order_by(Order.created_at.desc())
            .limit(10)
        )
        orders = orders_result.scalars().all()

    if not orders:
        text = (
            "📚 *Order History*\n\n"
            "_No orders found. Browse products to get started._"
        )
    else:
        lines = []
        for o in orders:
            status_text = {
                OrderStatus.FULFILLED.value: "Delivered",
                OrderStatus.PENDING.value: "Pending",
                OrderStatus.PAID.value: "Paid",
                OrderStatus.FAILED.value: "Failed",
                OrderStatus.REFUNDED.value: "Refunded",
            }.get(o.status, o.status.title())

            lines.append(
                f"• *{o.product_name}* ×{o.quantity}\n"
                f"  `₦{o.amount_ngn:,.2f}` • {o.created_at.strftime('%d %b %Y, %H:%M')} [{status_text}]"
            )

        orders_text = "\n\n".join(lines)
        text = (
            f"📚 *Order History* · {len(orders)} recent\n\n"
            f"{orders_text}"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Shop Subscriptions", callback_data="catalog", api_kwargs={"style": "primary"})],
        [InlineKeyboardButton("Main Menu", callback_data="start")],
    ])

    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def my_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /orders command."""
    await my_orders_callback(update, context)


# ═══════════════════════════════════════════════════════════════
#  Direct payment flow
# ═══════════════════════════════════════════════════════════════

async def direct_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'direct_pay:<product_id>:<quantity>[:<method>]' — generate Paystack payment link."""
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return
    logger.info(
        "Direct Pay callback received: tg_user=%s data=%s",
        update.effective_user.id, query.data,
    )
    await query.answer("Generating payment link…")

    parts = query.data.split(":")
    if len(parts) < 3:
        return
    try:
        product_id = int(parts[1])
        quantity = int(parts[2])
    except ValueError:
        logger.error("Direct Pay callback has invalid IDs: data=%s", query.data)
        if query.message:
            await query.message.edit_text("Invalid order selection. Please open the catalog again.")
        return
    method = parts[3] if len(parts) > 3 else "all"

    product_data = context.user_data.get("buy_product", {})
    telegram_id = update.effective_user.id
    if not product_data or product_data.get("id") != product_id:
        from bot.handlers.catalog import _get_products
        try:
            products = await _get_products(reseller_user_id=await _reseller_id(telegram_id))
            product = next((p for p in products if p.id == product_id), None)
            if product:
                product_data = {
                    "id": product.id,
                    "name": product.name,
                    "category": product.category,
                    "price_usd": product.price_usd,
                    "price_ngn": product.price_ngn,
                    "min_qty": product.min_qty,
                    "max_qty": product.max_qty,
                    "source": product.source,
                    "manual_product_id": product.manual_product_id,
                    "quantity": quantity,
                    "total_ngn": float(Decimal(str(product.price_ngn)) * quantity),
                    "total_usd": float(Decimal(str(product.price_usd)) * quantity),
                }
                context.user_data["buy_product"] = product_data
        except Exception:
            logger.exception("Direct Pay product recovery failed: tg_user=%s product_id=%s", telegram_id, product_id)
            product_data = {}

    if not product_data:
        if query.message:
            await query.message.edit_text("Session expired. Please browse products again.")
        return

    refreshed_product = await _refresh_purchase_product(telegram_id, product_id, quantity)
    if not refreshed_product:
        if query.message:
            await query.message.edit_text("⚠️ This product is no longer available. Please browse the catalog again.")
        return
    product_data = refreshed_product
    context.user_data["buy_product"] = product_data
    total_ngn = Decimal(str(product_data.get("total_ngn", 0)))
    total_usd = Decimal(str(product_data.get("total_usd", product_data.get("price_usd", 0) * quantity)))

    # Get user
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

    if not user:
        if query.message:
            await query.message.edit_text("User not found. Please /start.")
        return

    # Generate reference and payment link (matching deposit checkout flow)
    import uuid
    reference = f"ORD-{uuid.uuid4().hex[:12].upper()}"
    email = f"tg_{telegram_id}@drsub.bot"
    payment_url = ""

    try:
        from bot.services.paystack import paystack, PaystackError
        if not settings.paystack_secret_key:
            logger.error("Direct Pay blocked: Paystack secret is not configured ref=%s", reference)
            raise PaystackError(0, "Paystack secret key is not configured")

        payment_url = await paystack.initialize_transaction(
            email=email,
            amount_ngn=float(total_ngn),
            reference=reference,
            metadata={
                "type": "direct_order",
                "telegram_id": telegram_id,
                "user_id": user.id,
                "product_id": product_id,
                "product_name": product_data["name"],
                "quantity": quantity,
                "amount_usd": float(total_usd),
                "source": product_data.get("source", "subexpress"),
                "manual_product_id": product_data.get("manual_product_id"),
                "super_reseller": await _is_super_reseller(telegram_id),
            },
        )

        # Save pending order
        from bot.models.pending_order import PendingOrder
        async with async_session() as session:
            pending = PendingOrder(
                user_id=user.id,
                telegram_id=telegram_id,
                product_id=product_id,
                product_name=product_data["name"],
                quantity=quantity,
                amount_ngn=total_ngn,
                amount_usd=total_usd,
            )
            session.add(pending)

            # Also create a pending transaction for tracking
            tx = Transaction(
                user_id=user.id,
                type=TransactionType.PURCHASE.value,
                amount=total_ngn,
                status=TransactionStatus.PENDING.value,
                reference=reference,
                description=f"Direct pay: {product_data['name']} ×{quantity}",
            )
            session.add(tx)
            await session.commit()
        logger.info("Direct payment records saved: ref=%s tg_user=%s", reference, telegram_id)

        # Start background payment polling
        import asyncio
        from bot.services.payment_poller import poll_payment
        asyncio.create_task(poll_payment(
            reference=reference,
            metadata={
                "type": "direct_order",
                "telegram_id": telegram_id,
                "user_id": user.id,
                "product_id": product_id,
                "product_name": product_data["name"],
                "quantity": quantity,
                "amount_usd": float(total_usd),
                "source": product_data.get("source", "subexpress"),
                "manual_product_id": product_data.get("manual_product_id"),
                "super_reseller": await _is_super_reseller(telegram_id),
            },
        ))

        from bot.services.currency import get_user_currency, get_currency_info, format_dual_price
        curr_code = await get_user_currency(telegram_id)
        curr_info = get_currency_info(curr_code)
        total_display = format_dual_price(float(total_usd), curr_code, float(total_ngn))

        conversion_note = ""
        if curr_code != "NGN":
            conversion_note = (
                f"\n_Paystack processes this in NGN (₦{total_ngn:,.0f}). Your bank automatically debits your card in {curr_info.flag} {curr_info.name} ({curr_info.symbol})._\n"
            )

        text = (
            f"🏦 *Bank Transfer Checkout* · {curr_info.flag} {curr_info.code}\n\n"
            "📦 *Order details*\n"
            f"Product: *{_markdown_text(product_data['name'])}*\n"
            f"Quantity: `{quantity}`\n"
            f"Total payable: *`{total_display}`*\n"
            f"Reference: `{reference}`\n\n"
            "💳 *Payment method*\n"
            "Pay via bank transfer through Paystack.\n\n"
            f"[Proceed to Secure Paystack Checkout]({payment_url})\n"
            f"{conversion_note}\n"
            "Your subscription key will be delivered automatically in this chat upon payment confirmation."
        )

    except Exception as exc:
        logger.exception(
            "Direct payment init failed: tg_user=%s product_id=%s quantity=%s ref=%s error=%s",
            telegram_id, product_id, quantity, reference, exc,
        )
        text = "Could not generate payment link. Please try again."

    keyboard_buttons = []
    if payment_url:
        keyboard_buttons.append([InlineKeyboardButton("Open Payment Link", url=payment_url, api_kwargs={"style": "success"})])
    keyboard_buttons.append([InlineKeyboardButton("Change Payment Method", callback_data=f"back_to_order:{product_id}:{quantity}", api_kwargs={"style": "primary"})])
    keyboard_buttons.append([
        InlineKeyboardButton("Back to Catalog", callback_data="catalog"),
        InlineKeyboardButton("Main Menu", callback_data="start"),
    ])

    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    if query.message:
        await query.message.edit_text(
            text, parse_mode="Markdown", reply_markup=keyboard,
            disable_web_page_preview=True,
        )


async def wallet_insufficient_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user when they attempt wallet payment without enough funds."""
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 3:
        return
    product_id = int(parts[1])
    quantity = int(parts[2])

    product_data = context.user_data.get("buy_product", {})
    if not product_data or product_data.get("id") != product_id:
        from bot.handlers.catalog import _get_products
        try:
            products = await _get_products(reseller_user_id=await _reseller_id(update.effective_user.id))
            product = next((p for p in products if p.id == product_id), None)
            if product:
                product_data = {
                    "id": product.id,
                    "name": product.name,
                    "category": product.category,
                    "price_usd": product.price_usd,
                    "price_ngn": product.price_ngn,
                    "min_qty": product.min_qty,
                    "max_qty": product.max_qty,
                    "source": product.source,
                    "manual_product_id": product.manual_product_id,
                    "quantity": quantity,
                    "total_ngn": float(Decimal(str(product.price_ngn)) * quantity),
                    "total_usd": float(Decimal(str(product.price_usd)) * quantity),
                }
                context.user_data["buy_product"] = product_data
        except Exception:
            pass

    price_per_unit = product_data.get("price_ngn", 0)
    total_ngn = Decimal(str(price_per_unit)) * quantity

    user, wallet = await _get_user_and_wallet(update.effective_user.id)
    balance = wallet.balance if wallet else Decimal("0.00")
    deficit = total_ngn - balance

    text = (
        "⚠️ *Insufficient Wallet Balance*\n\n"
        "📦 *Order details*\n"
        f"Product: *{_markdown_text(product_data.get('name', 'Subscription'))}* ×{quantity}\n"
        f"Total order cost: `₦{total_ngn:,.2f}`\n"
        f"Wallet balance: `₦{balance:,.2f}`\n"
        f"Top-up needed: *`₦{deficit:,.2f}`*\n\n"
        "💳 *Choose a payment option*\n"
        "Choose an option below to complete your order:"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Deposit Funds to Wallet", callback_data="deposit", api_kwargs={"style": "success"})],
        [InlineKeyboardButton(f"Pay via Bank Transfer (₦{total_ngn:,.0f})", callback_data=f"direct_pay:{product_id}:{quantity}", api_kwargs={"style": "primary"})],
        [InlineKeyboardButton("Back to Order Summary", callback_data=f"back_to_order:{product_id}:{quantity}")],
    ])

    if query.message:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def back_to_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to order checkout summary screen."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 3:
        return
    product_id = int(parts[1])
    quantity = int(parts[2])

    product_data = context.user_data.get("buy_product", {})
    if not product_data or product_data.get("id") != product_id:
        from bot.handlers.catalog import _get_products
        try:
            products = await _get_products(reseller_user_id=await _reseller_id(update.effective_user.id))
            product = next((p for p in products if p.id == product_id), None)
            if product:
                product_data = {
                    "id": product.id,
                    "name": product.name,
                    "category": product.category,
                    "price_usd": product.price_usd,
                    "price_ngn": product.price_ngn,
                    "min_qty": product.min_qty,
                    "max_qty": product.max_qty,
                    "source": product.source,
                    "manual_product_id": product.manual_product_id,
                    "quantity": quantity,
                    "total_ngn": float(Decimal(str(product.price_ngn)) * quantity),
                    "total_usd": float(Decimal(str(product.price_usd)) * quantity),
                }
                context.user_data["buy_product"] = product_data
        except Exception:
            pass

    if query.message:
        await _show_confirmation(query.message, context, edit=True)


# ═══════════════════════════════════════════════════════════════
#  Register with the application
# ═══════════════════════════════════════════════════════════════

async def qty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'qty:<product_id>:<quantity>' — quick quantity selection."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 3:
        return

    product_id = int(parts[1])
    quantity = int(parts[2])

    product_data = context.user_data.get("buy_product")
    if not product_data or product_data.get("id") != product_id:
        if query.message:
            await query.message.edit_text("⚠️ Session expired. Browse products again.")
        return

    context.user_data["buy_product"]["quantity"] = quantity
    await _show_confirmation(query.message, context, edit=True)


def register(app) -> None:
    """Register all purchase-related handlers."""

    # Buy conversation handler (quantity input)
    buy_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(buy_callback, pattern=r"^buy:"),
        ],
        states={
            AWAITING_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_received),
                CallbackQueryHandler(qty_callback, pattern=r"^qty:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", buy_cancel),
            CallbackQueryHandler(qty_callback, pattern=r"^qty:"),
        ],
    )

    app.add_handler(buy_conv)

    # Quick quantity callback (outside conversation too)
    app.add_handler(CallbackQueryHandler(qty_callback, pattern=r"^qty:"))

    # Confirm purchase (wallet) callback
    app.add_handler(CallbackQueryHandler(confirm_buy_callback, pattern=r"^confirm_buy:"))

    # Wallet insufficient callback
    app.add_handler(CallbackQueryHandler(wallet_insufficient_callback, pattern=r"^wallet_insufficient:"))

    # Return to order checkout callback
    app.add_handler(CallbackQueryHandler(back_to_order_callback, pattern=r"^back_to_order:"))

    # Direct payment callback
    app.add_handler(CallbackQueryHandler(direct_pay_callback, pattern=r"^direct_pay:"))

    # My Orders
    app.add_handler(CommandHandler("orders", my_orders_command))
    app.add_handler(CallbackQueryHandler(my_orders_callback, pattern=r"^my_orders$"))
