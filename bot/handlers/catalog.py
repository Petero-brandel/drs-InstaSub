"""
DRSub – Catalog handler.

Direct product listing starting from available and most popular products down to out-of-stock.
Each product is displayed in a solid horizontal box/button.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, delete
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from bot.config import settings
from bot.database import async_session
from bot.models.product_cache import ProductCache
from bot.models.user import User
from bot.services.subexpress import subexpress, Product, SubExpressError

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(minutes=10)
PAGE_SIZE = 10

# Popular products prioritized at the top of available items
POPULAR_KEYWORDS = [
    "chatgpt",
    "gpt",
    "canva",
    "capcut",
    "netflix",
    "prime",
    "spotify",
    "apple",
    "telegram",
    "vpn",
    "nord",
    "gmail",
    "youtube",
    "discord",
    "duolingo",
    "lovable",
    "crunchyroll",
]

BRAND_ICONS = [
    ("chatgpt", "🤖"),
    ("gpt", "🤖"),
    ("openai", "🤖"),
    ("capcut", "🎬"),
    ("canva", "🎨"),
    ("netflix", "🍿"),
    ("prime", "📺"),
    ("spotify", "🎵"),
    ("apple", "🍏"),
    ("telegram", "⭐"),
    ("vpn", "🛡️"),
    ("nord", "🛡️"),
    ("gmail", "📧"),
    ("google", "📧"),
    ("youtube", "▶️"),
    ("duolingo", "🦉"),
    ("lovable", "💖"),
    ("discord", "👾"),
    ("crunchyroll", "🍥"),
    ("office", "💼"),
    ("windows", "🪟"),
    ("steam", "🎮"),
    ("tradingview", "📈"),
    ("coursera", "🎓"),
    ("linkedin", "👔"),
]


def _product_icon(product: Product) -> str:
    """Resolve specific brand icon for a product."""
    name_lower = product.name.lower()
    for kw, icon in BRAND_ICONS:
        if kw in name_lower:
            return icon
    cat_lower = product.category.lower()
    for kw, icon in BRAND_ICONS:
        if kw in cat_lower:
            return icon
    return "⚡"


def _cat_icon(category: str) -> str:
    cat_lower = category.lower()
    for kw, icon in BRAND_ICONS:
        if kw in cat_lower:
            return icon
    return "📦"


def _sort_products(products: list[Product]) -> list[Product]:
    """Sort products: available & most popular first, then out of stock at bottom."""
    def sort_score(p: Product) -> tuple[int, int, str]:
        # 1. Stock status: In stock (1) comes before Out of stock (0)
        stock_score = 1 if p.in_stock else 0

        # 2. Popularity score (higher is better)
        name_lower = p.name.lower()
        popularity_score = 0
        for idx, kw in enumerate(POPULAR_KEYWORDS):
            if kw in name_lower:
                popularity_score = 100 - idx
                break

        # Invert scores for descending sort in Python:
        # We sort by (-stock_score, -popularity_score, p.name)
        return (-stock_score, -popularity_score, p.name)

    return sorted(products, key=sort_score)


# ═══════════════════════════════════════════════════════════════
#  Product caching & fetching
# ═══════════════════════════════════════════════════════════════

async def _sync_products() -> list[Product]:
    """Fetch products from SubExpress and cache them."""
    products = await subexpress.list_products()

    async with async_session() as session:
        await session.execute(delete(ProductCache))

        now = datetime.now(timezone.utc)
        for p in products:
            session.add(
                ProductCache(
                    product_id=p.id,
                    name=p.name,
                    category=p.category,
                    price_usd=p.price_usd,
                    description=p.description,
                    in_stock=p.in_stock,
                    min_qty=p.min_qty,
                    max_qty=p.max_qty,
                    raw_data=p.raw,
                    synced_at=now,
                )
            )
        await session.commit()
        logger.info("Product cache refreshed – %d products stored", len(products))

    return products


async def _get_products(force_refresh: bool = False, reseller_user_id: int | None = None) -> list[Product]:
    """Return products from cache with custom price overlay."""
    products: list[Product] = []

    if not force_refresh:
        async with async_session() as session:
            result = await session.execute(select(ProductCache).limit(1))
            sample = result.scalar_one_or_none()

            if sample and (datetime.now(timezone.utc) - sample.synced_at.replace(tzinfo=timezone.utc)) < CACHE_TTL:
                all_result = await session.execute(
                    select(ProductCache).order_by(ProductCache.name)
                )
                cached = all_result.scalars().all()
                products = [
                    Product(
                        id=c.product_id,
                        name=c.name,
                        category=c.category,
                        price_usd=c.price_usd,
                        in_stock=c.in_stock,
                        min_qty=c.min_qty,
                        max_qty=c.max_qty,
                        description=c.description or "",
                        raw=c.raw_data or {},
                    )
                    for c in cached
                ]

    if not products:
        try:
            products = await _sync_products()
        except Exception as exc:
            logger.warning("Could not sync live products from SubExpress (%s). Falling back to cache.", exc)
            async with async_session() as session:
                all_result = await session.execute(
                    select(ProductCache).order_by(ProductCache.name)
                )
                cached = all_result.scalars().all()
                if cached:
                    products = [
                        Product(
                            id=c.product_id,
                            name=c.name,
                            category=c.category,
                            price_usd=c.price_usd,
                            in_stock=c.in_stock,
                            min_qty=c.min_qty,
                            max_qty=c.max_qty,
                            description=c.description or "",
                            raw=c.raw_data or {},
                        )
                        for c in cached
                    ]
                else:
                    # If database is fresh (e.g. newly deployed Supabase), load seed catalog
                    import json
                    from pathlib import Path
                    seed_file = Path(__file__).resolve().parent.parent.parent / "seed_products.json"
                    if seed_file.exists():
                        try:
                            with open(seed_file, "r", encoding="utf-8") as f:
                                seed_data = json.load(f)
                            products = [
                                Product(
                                    id=p["id"],
                                    name=p["name"],
                                    category=p["category"],
                                    price_usd=p["price_usd"],
                                    in_stock=p["in_stock"],
                                    min_qty=p["min_qty"],
                                    max_qty=p["max_qty"],
                                    description=p["description"],
                                    raw=p,
                                )
                                for p in seed_data
                            ]
                            now = datetime.now(timezone.utc)
                            for p in products:
                                session.add(ProductCache(
                                    product_id=p.id,
                                    name=p.name,
                                    category=p.category,
                                    price_usd=p.price_usd,
                                    description=p.description,
                                    in_stock=p.in_stock,
                                    min_qty=p.min_qty,
                                    max_qty=p.max_qty,
                                    raw_data=p.raw,
                                    synced_at=now,
                                ))
                            await session.commit()
                        except Exception as seed_err:
                            logger.error("Failed to load seed products: %s", seed_err)
                    if not products:
                        raise

    # Manual products use negative IDs to share the existing catalog callbacks.
    from bot.models.manual_product import ManualProduct
    async with async_session() as session:
        manual_result = await session.execute(
            select(ManualProduct).order_by(ManualProduct.name)
        )
        for manual in manual_result.scalars().all():
            products.append(Product(
                id=-manual.id,
                name=manual.name,
                category="Manual Products",
                price_usd=0.0,
                in_stock=manual.in_stock,
                min_qty=1,
                max_qty=1,
                description=manual.description or "",
                raw={},
                custom_price_ngn=float(manual.price_ngn),
                source="manual",
                manual_product_id=manual.id,
            ))

    # Overlay custom and reseller prices.
    from bot.models.custom_price import CustomPrice
    async with async_session() as session:
        result = await session.execute(select(CustomPrice))
        custom_map = {cp.product_id: cp.price_ngn for cp in result.scalars().all()}
        reseller_map = {}
        if reseller_user_id is not None:
            user_result = await session.execute(select(User).where(User.id == reseller_user_id))
            reseller = user_result.scalar_one_or_none()
            if reseller and reseller.is_super_reseller:
                from bot.models.super_reseller import SuperResellerPrice
                reseller_result = await session.execute(select(SuperResellerPrice))
                reseller_rows = reseller_result.scalars().all()
                reseller_map = {(rp.source, rp.product_id): rp.price_ngn for rp in reseller_rows}
                reseller_name_map = {
                    (rp.source, rp.product_name.strip().casefold()): rp.price_ngn
                    for rp in reseller_rows if rp.product_name
                }
            elif reseller and reseller.is_reseller:
                from bot.models.reseller import ResellerPrice
                reseller_result = await session.execute(select(ResellerPrice))
                reseller_rows = reseller_result.scalars().all()
                reseller_map = {(rp.source, rp.product_id): rp.price_ngn for rp in reseller_rows}
                reseller_name_map = {
                    (rp.source, rp.product_name.strip().casefold()): rp.price_ngn
                    for rp in reseller_rows if rp.product_name
                }
            else:
                reseller_name_map = {}
        else:
            reseller_name_map = {}

    for p in products:
        custom = custom_map.get(p.id)
        if custom is not None:
            p.custom_price_ngn = float(custom)
        reseller_price = reseller_map.get((p.source, p.manual_product_id or p.id))
        if reseller_price is None:
            reseller_price = reseller_name_map.get((p.source, p.name.strip().casefold()))
        if reseller_price is not None:
            p.reseller_price_ngn = float(reseller_price)

    return products


# ═══════════════════════════════════════════════════════════════
#  Keyboards
# ═══════════════════════════════════════════════════════════════

def _build_product_list_keyboard(
    products: list[Product],
    page: int = 0,
    curr_code: str = "NGN",
) -> InlineKeyboardMarkup:
    """Build solid horizontal button layout for products with green/red styles."""
    from bot.services.currency import format_price, get_currency_info

    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    page_products = products[start_idx:end_idx]
    total_pages = max(1, (len(products) + PAGE_SIZE - 1) // PAGE_SIZE)

    buttons: list[list[InlineKeyboardButton]] = []

    # Each product is a single solid horizontal colored button row
    for p in page_products:
        if p.in_stock:
            price_str = format_price(p.price_usd, curr_code, p.price_ngn)
            label = f"{p.name} — {price_str}"
            buttons.append([InlineKeyboardButton(
                label,
                callback_data=f"prod:{p.id}",
                api_kwargs={"style": "success"},
            )])
        else:
            label = f"{p.name} — Out of Stock"
            buttons.append([InlineKeyboardButton(
                label,
                callback_data=f"prod:{p.id}",
                api_kwargs={"style": "danger"},
            )])

    # Pagination navigation
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("Prev", callback_data=f"cat_page:{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if end_idx < len(products):
        nav_row.append(InlineKeyboardButton("Next", callback_data=f"cat_page:{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    info = get_currency_info(curr_code)
    buttons.append([
        InlineKeyboardButton(f"{info.flag} Currency: {info.code} ({info.symbol})", callback_data="change_currency"),
    ])
    buttons.append([
        InlineKeyboardButton("Refresh", callback_data="catalog_refresh"),
        InlineKeyboardButton("Main Menu", callback_data="start", api_kwargs={"style": "primary"}),
    ])

    return InlineKeyboardMarkup(buttons)


def _build_product_detail_keyboard(product: Product, curr_code: str = "NGN") -> InlineKeyboardMarkup:
    """Build detail view buttons (no emojis)."""
    from bot.services.currency import format_price

    price_str = format_price(product.price_usd, curr_code, product.price_ngn)
    buttons: list[list[InlineKeyboardButton]] = []
    if product.in_stock:
        buttons.append([InlineKeyboardButton(
            f"Pay Now • {price_str}",
            callback_data=f"buy:{product.id}",
            api_kwargs={"style": "success"},
        )])
    else:
        buttons.append([InlineKeyboardButton(
            "Out of Stock",
            callback_data="noop",
            api_kwargs={"style": "danger"},
        )])

    buttons.append([
        InlineKeyboardButton("Back to Catalog", callback_data="catalog", api_kwargs={"style": "primary"}),
        InlineKeyboardButton("Main Menu", callback_data="start"),
    ])
    return InlineKeyboardMarkup(buttons)


# ═══════════════════════════════════════════════════════════════
#  Handlers
# ═══════════════════════════════════════════════════════════════

async def _show_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    edit: bool = False,
) -> None:
    """Load products and display them directly, in-stock first down to out-of-stock."""
    try:
        raw_products = await _get_products()
    except SubExpressError as exc:
        logger.error("Failed to load products: %s", exc)
        text = "Could not load catalog. Please try again later."
        msg = update.callback_query.message if update.callback_query else update.effective_message
        if msg:
            if edit:
                await msg.edit_text(text)
            else:
                await msg.reply_text(text)
        return

    if not raw_products:
        text = "No products available right now."
        msg = update.callback_query.message if update.callback_query else update.effective_message
        if msg:
            if edit:
                await msg.edit_text(text)
            else:
                await msg.reply_text(text)
        return

    # Sort: available & most popular first, out of stock at bottom
    sorted_products = _sort_products(raw_products)

    total_count = len(sorted_products)
    in_stock_count = sum(1 for p in sorted_products if p.in_stock)

    tg_user = update.effective_user
    from bot.services.currency import get_user_currency, get_currency_info
    curr_code = await get_user_currency(tg_user.id) if tg_user else "NGN"
    if tg_user:
        async with async_session() as session:
            user_row = await session.scalar(select(User).where(User.telegram_id == tg_user.id))
            reseller_id = user_row.id if user_row and user_row.is_reseller else None
        if reseller_id:
            raw_products = await _get_products(reseller_user_id=reseller_id)
            sorted_products = _sort_products(raw_products)
            total_count = len(sorted_products)
            in_stock_count = sum(1 for p in sorted_products if p.in_stock)
    curr_info = get_currency_info(curr_code)

    text = (
        f"*DRSub Store ({curr_info.flag} {curr_info.code})*\n"
        "\n"
        f"Available: *{in_stock_count}* items  •  Total: *{total_count}*\n"
        "Instant key and account delivery\n"
        "\n"
        "Select any subscription below to view details and buy:"
    )

    keyboard = _build_product_list_keyboard(sorted_products, page, curr_code=curr_code)
    msg = update.callback_query.message if update.callback_query else update.effective_message

    if msg:
        if edit:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def products_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /products and /shop commands."""
    await _show_products(update, context, page=0, edit=False)


async def catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'catalog' callback."""
    query = update.callback_query
    if query:
        await query.answer()
    await _show_products(update, context, page=0, edit=query is not None)


async def catalog_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pagination callback 'cat_page:<page>'."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":")
    page = int(parts[1]) if len(parts) > 1 else 0
    await _show_products(update, context, page=page, edit=True)


async def catalog_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-refresh product cache and display catalog."""
    query = update.callback_query
    if query:
        await query.answer("Refreshing catalog…")

    try:
        await _sync_products()
    except Exception as exc:
        logger.warning("Could not sync live products (%s). Falling back to cache.", exc)

    await _show_products(update, context, page=0, edit=True)


async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show clean product detail view (no emojis)."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 2:
        return
    product_id = int(parts[1])

    try:
        reseller_id = None
        if tg_user := update.effective_user:
            async with async_session() as session:
                user_row = await session.scalar(select(User).where(User.telegram_id == tg_user.id))
                reseller_id = user_row.id if user_row and user_row.is_reseller else None
        all_products = await _get_products(reseller_user_id=reseller_id)
    except SubExpressError as exc:
        logger.error("Failed to load products: %s", exc)
        if query.message:
            await query.message.edit_text("Could not load product.")
        return

    product = next((p for p in all_products if p.id == product_id), None)
    if not product:
        if query.message:
            await query.message.edit_text(
                "Product not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Back to Catalog", callback_data="catalog")]]
                ),
            )
        return

    stock_badge = "*In Stock (Ready to Deliver)*" if product.in_stock else "*Temporarily Out of Stock*"
    desc_line = f"\n_{product.description}_\n" if product.description else ""

    tg_user = update.effective_user
    from bot.services.currency import get_user_currency, format_dual_price
    curr_code = await get_user_currency(tg_user.id) if tg_user else "NGN"
    price_display = format_dual_price(product.price_usd, curr_code, product.price_ngn)

    text = (
        f"*{product.name}*\n"
        f"Category: {product.category}\n"
        f"{desc_line}\n"
        "\n"
        f"Price: *{price_display}*\n"
        f"Status: {stock_badge}\n"
        "Delivery: Instant Automated Key / Account\n"
        "\n"
        "Tap Pay Now below to choose your payment method (Wallet or Card / USSD / Other)."
    )

    keyboard = _build_product_detail_keyboard(product, curr_code=curr_code)

    if query.message:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle no-op callback."""
    query = update.callback_query
    if query:
        await query.answer("This item is currently out of stock.", show_alert=True)


# ═══════════════════════════════════════════════════════════════
#  Register
# ═══════════════════════════════════════════════════════════════

def register(app) -> None:
    """Register all catalog handlers."""
    app.add_handler(CommandHandler("products", products_command))
    app.add_handler(CommandHandler("shop", products_command))

    app.add_handler(CallbackQueryHandler(catalog_callback, pattern=r"^catalog$"))
    app.add_handler(CallbackQueryHandler(catalog_refresh_callback, pattern=r"^catalog_refresh$"))
    app.add_handler(CallbackQueryHandler(catalog_page_callback, pattern=r"^cat_page:"))
    app.add_handler(CallbackQueryHandler(product_detail_callback, pattern=r"^prod:"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
