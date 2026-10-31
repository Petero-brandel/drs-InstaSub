"""
DRSub – /start command handler.

Premium welcome experience with bot commands menu and callback routing.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from bot.config import settings
from bot.database import async_session
from bot.models.user import User

logger = logging.getLogger(__name__)


# ── Premium welcome message ──────────────────────────────────

def _welcome_text(first_name: str) -> str:
    return (
        f"*DRS InstaSub Marketplace*\n"
        "\n"
        f"Welcome, {first_name}.\n"
        "Automated digital subscriptions and tool licenses.\n"
        "\n"
        "• *Shop* — Browse subscriptions and instant licenses\n"
        "• *Deposit* — Fast automated wallet funding via Paystack\n"
        "• *Profile* — Check wallet balance and license keys\n"
        "• *Support* — 24/7 dedicated customer assistance\n\n"
        "Channel: @drsubchannel  •  Support: @drsubadmin\n\n"
        "Select an option below to proceed:"
    )


MAIN_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("Browse Available Products", callback_data="catalog", api_kwargs={"style": "success"}),
        InlineKeyboardButton("Deposit Funds", callback_data="deposit", api_kwargs={"style": "primary"}),
    ],
    [
        InlineKeyboardButton("Profile & Wallet", callback_data="wallet"),
        InlineKeyboardButton("Support & Help", callback_data="help"),
    ],
    [
        InlineKeyboardButton("📢 Referrals", callback_data="referral"),
        InlineKeyboardButton("Currency / Region", callback_data="change_currency"),
    ],
])

ADMIN_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("Browse Available Products", callback_data="catalog", api_kwargs={"style": "success"}),
        InlineKeyboardButton("Deposit Funds", callback_data="deposit", api_kwargs={"style": "primary"}),
    ],
    [
        InlineKeyboardButton("Profile & Wallet", callback_data="wallet"),
        InlineKeyboardButton("Support & Help", callback_data="help"),
    ],
    [
        InlineKeyboardButton("📢 Referrals", callback_data="referral"),
        InlineKeyboardButton("Currency / Region", callback_data="change_currency"),
    ],
    [
        InlineKeyboardButton("Admin Console", callback_data="admin_panel", api_kwargs={"style": "danger"}),
    ],
])


# ── Bot commands menu ────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("start", "Main menu"),
    BotCommand("shop", "Browse subscriptions"),
    BotCommand("wallet", "Profile and wallet"),
    BotCommand("referral", "Refer & earn commissions"),
    BotCommand("currency", "Change currency / region"),
    BotCommand("deposit", "Deposit funds"),
    BotCommand("orders", "Order history"),
    BotCommand("help", "Support and guide"),
]

ADMIN_COMMANDS = [
    BotCommand("admin", "Admin console"),
    BotCommand("setprice", "Manage product prices"),
    BotCommand("addmanual", "Add a manual product"),
    BotCommand("manuals", "List manual products"),
    BotCommand("editmanual", "Edit a manual product"),
    BotCommand("deletemanual", "Delete a manual product"),
    BotCommand("fulfill", "Fulfill a manual order"),
    BotCommand("fund", "Credit user wallet"),
    BotCommand("broadcast", "Send announcement"),
]


# ── Handler logic ────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command.

    Deep-link support: /start ref_ABCDEF links the new user to a referrer.
    """
    tg_user = update.effective_user
    if tg_user is None or update.effective_message is None:
        return

    # Parse deep link payload (e.g. /start ref_ABCDEF)
    referral_code: str | None = None
    if context.args and len(context.args) > 0:
        payload = context.args[0]
        if payload.startswith("ref_"):
            referral_code = payload[4:]  # Strip 'ref_' prefix

    is_new_user = False
    async with async_session() as session:
        stmt = select(User).where(User.telegram_id == tg_user.id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
            )
            session.add(user)
            await session.commit()
            is_new_user = True
            logger.info("New user registered: %s (tg_id=%d)", tg_user.username, tg_user.id)
        else:
            user.username = tg_user.username
            user.first_name = tg_user.first_name
            user.last_name = tg_user.last_name
            await session.commit()

    # Process referral link (only for new users or users without a referrer)
    if referral_code:
        from bot.services.referral import register_referral, get_user_by_referral_code
        registered = await register_referral(tg_user.id, referral_code)
        if registered:
            logger.info("Referral registered for user tg=%d code=%s", tg_user.id, referral_code)
            try:
                ref_user = await get_user_by_referral_code(referral_code)
                if ref_user:
                    joined_name = tg_user.first_name or "A new user"
                    await context.bot.send_message(
                        chat_id=ref_user.telegram_id,
                        text=(
                            f"🎉 *New Referral!*\n\n"
                            f"*{joined_name}* just joined DRSub using your link!\n"
                            "You will automatically earn commissions on every subscription they purchase."
                        ),
                        parse_mode="Markdown",
                    )
            except Exception as ref_err:
                logger.warning("Could not send referral notification to referrer: %s", ref_err)

    # Choose keyboard based on admin status
    is_admin = tg_user.id in settings.admin_id_list
    keyboard = ADMIN_KEYBOARD if is_admin else MAIN_KEYBOARD

    await update.effective_message.reply_text(
        _welcome_text(tg_user.first_name or "there"),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'start' callback — return to main menu."""
    query = update.callback_query
    if query:
        await query.answer()

    tg_user = update.effective_user
    if not tg_user:
        return

    is_admin = tg_user.id in settings.admin_id_list
    keyboard = ADMIN_KEYBOARD if is_admin else MAIN_KEYBOARD

    if query and query.message:
        await query.message.edit_text(
            _welcome_text(tg_user.first_name or "there"),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help information."""
    query = update.callback_query
    if query:
        await query.answer()

    text = (
        "*DRS InstaSub Support & Guide*\n"
        "\n"
        "How to purchase:\n"
        "1. Tap *Deposit Funds* to fund your wallet via Paystack\n"
        "2. Tap *Shop Subscriptions* to select a tool or service\n"
        "3. Complete payment to receive your license key instantly\n\n"
        "Accepted Payment Methods:\n"
        "• Debit / Credit Card (Mastercard, Visa, Verve)\n"
        "• Direct Bank Transfer (Instant virtual account)\n"
        "• USSD\n\n"
        "Official Channels:\n"
        "• Support Desk: @drsubadmin\n"
        "• News & Updates: @drsubchannel\n"
        ""
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Main Menu", callback_data="start", api_kwargs={"style": "primary"})],
    ])

    msg = query.message if query else update.effective_message
    if msg:
        if query:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await help_callback(update, context)


async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /shop command — redirect to catalog."""
    from bot.handlers.catalog import catalog_callback
    await catalog_callback(update, context)


async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /deposit command."""
    from bot.handlers.wallet import deposit_start
    await deposit_start(update, context)


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin_panel callback."""
    from bot.handlers.admin import admin_command
    await admin_command(update, context)


async def currency_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /currency command."""
    await show_currency_menu(update, context, edit=False)


async def change_currency_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'change_currency' callback."""
    query = update.callback_query
    if query:
        await query.answer()
    await show_currency_menu(update, context, edit=query is not None)


async def show_currency_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    """Render currency selection menu."""
    tg_user = update.effective_user
    if not tg_user:
        return
    from bot.services.currency import get_user_currency, build_currency_keyboard, get_currency_info
    curr_code = await get_user_currency(tg_user.id)
    info = get_currency_info(curr_code)

    text = (
        "*Currency & Region*\n"
        f"Active Currency: *{info.flag} {info.name} ({info.symbol})*\n\n"
        "Select your preferred country / currency below:\n"
        "All subscription prices in the store and checkout will automatically display in your selected currency."
    )
    keyboard = build_currency_keyboard(curr_code)
    msg = update.callback_query.message if update.callback_query else update.effective_message
    if msg:
        if edit:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def set_currency_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'set_curr:<CODE>' callback."""
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return
    code = query.data.split(":")[1]
    from bot.services.currency import set_user_currency, get_currency_info
    await set_user_currency(update.effective_user.id, code)
    info = get_currency_info(code)
    await query.answer(f"Currency updated to {info.flag} {info.code}!", show_alert=True)
    await show_currency_menu(update, context, edit=True)


# ── Referral handlers ────────────────────────────────────────

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /referral command."""
    await _show_referral_panel(update, context, edit=False)


async def referral_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'referral' callback button."""
    query = update.callback_query
    if query:
        await query.answer()
    await _show_referral_panel(update, context, edit=query is not None)


async def _show_referral_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    edit: bool = False,
) -> None:
    """Show the referral stats panel with the user's unique link & code."""
    tg_user = update.effective_user
    if not tg_user:
        return

    from bot.services.referral import ensure_referral_code, get_referral_stats

    # Ensure the user has a referral code
    try:
        code = await ensure_referral_code(tg_user.id)
    except Exception:
        msg = update.callback_query.message if update.callback_query else update.effective_message
        if msg:
            t = "⚠️ Please /start first."
            if edit:
                await msg.edit_text(t)
            else:
                await msg.reply_text(t)
        return

    # Get stats
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == tg_user.id)
        )
        user = result.scalar_one_or_none()

    if not user:
        return

    from urllib.parse import quote
    stats = await get_referral_stats(user.id)
    total_referrals = stats["total_referrals"]
    total_earnings = stats["total_earnings"]
    recent = stats["recent_earnings"]

    # Build referral link
    ref_link = f"https://t.me/{settings.bot_username}?start=ref_{code}"
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote('Get instant, discounted premium subscriptions & tools on DRS InstaSub!')}"

    # Recent earnings lines
    earning_lines = []
    for e in recent[:5]:
        earning_lines.append(
            f"• ₦{e.amount:,.2f} — _{e.product_name}_ ({e.created_at.strftime('%d %b')})"
        )
    earnings_text = "\n".join(earning_lines) if earning_lines else "_No earnings yet — share your link!_"

    text = (
        "*📢 Referral Program*\n"
        "Earn *₦500 bonus* on every purchase\n"
        "made by users you refer — forever!\n"
        f"🔗 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        f"🏷️ *Your Code:* `{code}`\n\n"
        f"👥 *Total Referrals:* `{total_referrals}`\n"
        f"💰 *Total Earnings:* `₦{total_earnings:,.2f}`\n"
        "\n"
        "*Recent Earnings:*\n"
        f"{earnings_text}\n"
        "\n"
        f"_Share your link with friends. When they sign up and\n"
        "make a purchase, you earn ₦500 instantly!_"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Copy Link", callback_data="referral_copy"),
            InlineKeyboardButton("📤 Share Link", url=share_url),
        ],
        [
            InlineKeyboardButton("Profile & Wallet", callback_data="wallet"),
            InlineKeyboardButton("Main Menu", callback_data="start"),
        ],
    ])

    msg = update.callback_query.message if update.callback_query else update.effective_message
    if msg:
        if edit:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard,
                                disable_web_page_preview=True)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard,
                                 disable_web_page_preview=True)


async def referral_copy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'copy link' callback — show alert with copy instructions."""
    query = update.callback_query
    if query:
        tg_user = update.effective_user
        if tg_user:
            from bot.services.referral import ensure_referral_code
            try:
                code = await ensure_referral_code(tg_user.id)
                ref_link = f"https://t.me/{settings.bot_username}?start=ref_{code}"
                await query.answer(
                    f"Your link:\n{ref_link}\n\nTap and hold the link in the message to copy!",
                    show_alert=True,
                )
            except Exception:
                await query.answer("Could not load your referral code.")
        else:
            await query.answer()


# ── Setup bot commands on startup ────────────────────────────

async def setup_commands(app) -> None:
    """Register bot commands menu with Telegram."""
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        for admin_id in settings.admin_id_list:
            await app.bot.set_my_commands(
                ADMIN_COMMANDS,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        logger.info("Bot commands menu registered ✓")
    except Exception as exc:
        logger.warning("Could not set bot commands: %s", exc)


# ── Register ─────────────────────────────────────────────────

def register(app) -> None:
    """Register the /start handler with the Application."""
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("shop", shop_command))
    app.add_handler(CommandHandler("currency", currency_command))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CallbackQueryHandler(start_callback, pattern=r"^start$"))
    app.add_handler(CallbackQueryHandler(help_callback, pattern=r"^help$"))
    app.add_handler(CallbackQueryHandler(referral_callback, pattern=r"^referral$"))
    app.add_handler(CallbackQueryHandler(referral_copy_callback, pattern=r"^referral_copy$"))
    app.add_handler(CallbackQueryHandler(change_currency_callback, pattern=r"^change_currency$"))
    app.add_handler(CallbackQueryHandler(set_currency_callback, pattern=r"^set_curr:"))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern=r"^admin_panel$"))
