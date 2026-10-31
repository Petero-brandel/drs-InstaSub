"""
DRSub – Wallet handler (Premium UI).

Wallet view, deposit flow with quick-amount buttons,
and styled transaction history.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    Update,
)
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
from bot.services.paystack import paystack, PaystackError

logger = logging.getLogger(__name__)

AWAITING_DEPOSIT_AMOUNT = 0

# Quick deposit amounts (NGN)
QUICK_AMOUNTS = [500, 1000, 2000, 5000, 10000, 20000]


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

async def _get_or_create_wallet(user_id: int) -> Wallet:
    async with async_session() as session:
        result = await session.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )
        wallet = result.scalar_one_or_none()
        if wallet is None:
            wallet = Wallet(user_id=user_id, balance=Decimal("0.00"))
            session.add(wallet)
            await session.commit()
            await session.refresh(wallet)
    return wallet


async def _get_user_by_telegram_id(telegram_id: int) -> User | None:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()


async def _get_recent_transactions(user_id: int, limit: int = 5) -> list[Transaction]:
    async with async_session() as session:
        result = await session.execute(
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


# ═══════════════════════════════════════════════════════════════
#  Wallet view (premium styled)
# ═══════════════════════════════════════════════════════════════

async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_wallet(update, context)


async def wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
    await _show_wallet(update, context, edit=query is not None)


async def _show_wallet(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    edit: bool = False,
) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return

    user = await _get_user_by_telegram_id(tg_user.id)
    if not user:
        msg = update.callback_query.message if update.callback_query else update.effective_message
        if msg:
            text = "⚠️ Please send /start first."
            if edit:
                await msg.edit_text(text)
            else:
                await msg.reply_text(text)
        return

    wallet = await _get_or_create_wallet(user.id)
    transactions = await _get_recent_transactions(user.id)

    # Transaction lines
    tx_lines = []
    for tx in transactions:
        is_credit = tx.type in (
            TransactionType.DEPOSIT.value,
            TransactionType.REFUND.value,
            TransactionType.ADMIN_CREDIT.value,
        )
        sign = "+" if is_credit else "-"
        status = "Completed" if tx.status == TransactionStatus.COMPLETED.value else "Pending"

        desc = tx.description or tx.type
        if len(desc) > 25:
            desc = desc[:22] + "…"

        tx_lines.append(f"• `{sign}₦{tx.amount:,.2f}` — {desc} [{status}]")

    tx_history = "\n".join(tx_lines) if tx_lines else "_No transactions yet_"

    from bot.services.currency import get_user_currency, get_currency_info, convert_ngn_to_local
    curr_code = await get_user_currency(user.telegram_id)
    curr_info = get_currency_info(curr_code)

    balance_display = f"₦{wallet.balance:,.2f}"
    if curr_code != "NGN":
        _, local_bal_str = convert_ngn_to_local(float(wallet.balance), curr_code)
        balance_display = f"{local_bal_str} (~₦{wallet.balance:,.2f})"

    text = (
        f"*Account & Wallet ({curr_info.flag} {curr_info.code})*\n"
        "\n"
        f"• User: *{user.first_name or 'Customer'}*\n"
        f"• Telegram ID: `{user.telegram_id}`\n"
        f"• Wallet Balance: *`{balance_display}`*\n"
        f"• Active Currency: *{curr_info.flag} {curr_info.name} ({curr_info.symbol})*\n"
        "\n"
        "*Recent Activity:*\n"
        f"{tx_history}\n"
        ""
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Deposit Funds", callback_data="deposit", api_kwargs={"style": "success"}),
            InlineKeyboardButton("Shop Subscriptions", callback_data="catalog", api_kwargs={"style": "primary"}),
        ],
        [
            InlineKeyboardButton(f"Currency: {curr_info.flag} {curr_info.code}", callback_data="change_currency"),
            InlineKeyboardButton("Order History", callback_data="my_orders"),
        ],
        [
            InlineKeyboardButton("📢 Referrals", callback_data="referral"),
            InlineKeyboardButton("Main Menu", callback_data="start"),
        ],
    ])

    msg = update.callback_query.message if update.callback_query else update.effective_message
    if msg:
        if edit:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ═══════════════════════════════════════════════════════════════
#  Deposit flow with quick-amount keyboard
# ═══════════════════════════════════════════════════════════════

def _deposit_keyboard() -> ReplyKeyboardMarkup:
    """Build a custom reply keyboard with quick deposit amounts."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("₦500"), KeyboardButton("₦1,000"), KeyboardButton("₦2,000")],
            [KeyboardButton("₦5,000"), KeyboardButton("₦10,000"), KeyboardButton("₦20,000")],
            [KeyboardButton("Cancel")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start deposit flow with quick-select keyboard."""
    query = update.callback_query
    if query:
        await query.answer()

    text = (
        "*Deposit Funds*\n"
        "\n"
        "Select a quick deposit amount or reply with any amount (e.g. `7500`):\n\n"
        "• Minimum Deposit: `₦500.00`\n"
        "• Maximum Deposit: `₦500,000.00`\n"
        "• Processing: Instant automated credit via Paystack\n"
        ""
    )

    msg = query.message if query else update.effective_message
    if msg:
        await msg.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=_deposit_keyboard(),
        )

    return AWAITING_DEPOSIT_AMOUNT


async def deposit_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process deposit amount (from quick button or typed)."""
    if not update.effective_message or not update.effective_user:
        return ConversationHandler.END

    raw_text = update.effective_message.text or ""

    # Handle cancel
    if raw_text.strip().lower() in ("cancel", "❌ cancel", "/cancel"):
        await update.effective_message.reply_text(
            "Deposit cancelled.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    # Clean input
    cleaned = raw_text.replace("₦", "").replace(",", "").replace(" ", "").strip()

    try:
        amount = Decimal(cleaned)
    except Exception:
        await update.effective_message.reply_text(
            "Invalid amount. Tap a button or type a number like `5000`.",
            parse_mode="Markdown",
            reply_markup=_deposit_keyboard(),
        )
        return AWAITING_DEPOSIT_AMOUNT

    if amount < 500:
        await update.effective_message.reply_text(
            "Minimum deposit is ₦500.",
            reply_markup=_deposit_keyboard(),
        )
        return AWAITING_DEPOSIT_AMOUNT

    if amount > 500_000:
        await update.effective_message.reply_text(
            "Maximum deposit is ₦500,000.",
            reply_markup=_deposit_keyboard(),
        )
        return AWAITING_DEPOSIT_AMOUNT

    tg_user = update.effective_user
    user = await _get_user_by_telegram_id(tg_user.id)
    if not user:
        await update.effective_message.reply_text(
            "Please /start first.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    if not settings.paystack_secret_key:
        await update.effective_message.reply_text(
            "Payment system not configured.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    reference = f"DEP-{uuid.uuid4().hex[:12].upper()}"
    email = f"tg_{tg_user.id}@drsub.bot"

    try:
        payment_url = await paystack.initialize_transaction(
            email=email,
            amount_ngn=float(amount),
            reference=reference,
            metadata={
                "type": "wallet_deposit",
                "telegram_id": tg_user.id,
                "user_id": user.id,
            },
        )

        async with async_session() as session:
            tx = Transaction(
                user_id=user.id,
                type=TransactionType.DEPOSIT.value,
                amount=amount,
                status=TransactionStatus.PENDING.value,
                reference=reference,
                description=f"Deposit ₦{amount:,.2f}",
            )
            session.add(tx)
            await session.commit()

        # Start payment poller
        from bot.services.payment_poller import poll_payment
        asyncio.create_task(poll_payment(
            reference=reference,
            metadata={
                "type": "wallet_deposit",
                "telegram_id": tg_user.id,
                "user_id": user.id,
            },
        ))

        text = (
            "*Deposit Checkout*\n"
            "\n"
            f"• Amount: *`₦{amount:,.2f}`*\n"
            f"• Reference: `{reference}`\n"
            "• Channels: Card, Bank Transfer, USSD\n"
            "\n"
            f"[Proceed to Secure Paystack Checkout]({payment_url})\n\n"
            "Your wallet balance will be credited automatically upon confirmation."
        )

    except PaystackError as exc:
        logger.error("Payment init failed: %s", exc)
        text = "Could not generate payment link. Please try again."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Open Payment Link", url=payment_url, api_kwargs={"style": "success"})],
        [InlineKeyboardButton("My Wallet", callback_data="wallet", api_kwargs={"style": "primary"})],
        [InlineKeyboardButton("Main Menu", callback_data="start")],
    ])

    await update.effective_message.reply_text(
        text, parse_mode="Markdown", reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    return ConversationHandler.END


async def deposit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the deposit flow."""
    if update.effective_message:
        await update.effective_message.reply_text(
            "❌ Deposit cancelled.",
            reply_markup=ReplyKeyboardRemove(),
        )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
#  Register
# ═══════════════════════════════════════════════════════════════

def register(app) -> None:
    """Register all wallet handlers."""

    # Deposit conversation
    deposit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(deposit_start, pattern=r"^deposit$"),
            CommandHandler("deposit", deposit_start),
        ],
        states={
            AWAITING_DEPOSIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount_received),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", deposit_cancel),
        ],
    )
    app.add_handler(deposit_conv)

    # Wallet view
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CallbackQueryHandler(wallet_callback, pattern=r"^wallet$"))
