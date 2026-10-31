"""
DRSub – Forex & Exchange Rate Service.

Dynamically fetches trusted USD exchange rates everyday from reputable sources
with automatic multi-tier failover:
    1. Primary: Bybit P2P median advertiser rate for USDT/NGN
    2. Secondary: ExchangeRate-API (open.er-api.com)
    3. Tertiary: Open Currency API, then CoinGecko
  4. Local DB & .env fallback

Automatically updates:
  - settings.usd_to_ngn_rate (SubExpress orders & wholesale calculations)
  - bot.services.currency.CURRENCIES (NGN, GHS, KES, ZAR customer displays)
  - Database cache & audit logs
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

import httpx
from sqlalchemy import select

from bot.config import settings
from bot.database import async_session
from bot.models.exchange_rate import ExchangeRate, ExchangeRateConfig

logger = logging.getLogger(__name__)

# ── Trusted Exchange Rate Providers ──────────────────────────
PROVIDER_PRIMARY = "https://open.er-api.com/v6/latest/USD"
PROVIDER_BYBIT_P2P = "https://api2.bybit.com/fiat/otc/item/online"
PROVIDER_SECONDARY = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
PROVIDER_TERTIARY = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=ngn,ghs,kes,zar"

# Target currency codes we track
TRACKED_CURRENCIES = ["NGN", "GHS", "KES", "ZAR"]

# Scheduler interval: 12 hours (syncs twice daily, picking up new forex closes)
FOREX_SYNC_INTERVAL_SECONDS = 12 * 3600

# Background task reference
_forex_task: asyncio.Task | None = None


# ── Provider Fetchers ────────────────────────────────────────

async def _fetch_from_primary(client: httpx.AsyncClient) -> tuple[dict[str, float], str]:
    """Fetch from ExchangeRate-API (official daily central bank / forex feed)."""
    resp = await client.get(PROVIDER_PRIMARY, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()

    if data.get("result") != "success" or "rates" not in data:
        raise ValueError("Invalid response structure from ExchangeRate-API")

    rates_raw = data["rates"]
    rates = {}
    for code in TRACKED_CURRENCIES:
        if code in rates_raw:
            rates[code] = float(rates_raw[code])

    if "NGN" not in rates:
        raise ValueError("NGN rate missing from ExchangeRate-API")

    return rates, "ExchangeRate-API"


async def _fetch_from_bybit(client: httpx.AsyncClient) -> tuple[dict[str, float], str]:
    """Fetch the median advertised USDT/NGN P2P price from Bybit."""
    resp = await client.get(
        PROVIDER_BYBIT_P2P,
        params={
            "tokenId": "USDT",
            "currencyId": "NGN",
            "payment": "",
            "side": "1",
            "size": "50",
            "page": "1",
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("result", {}).get("items", [])
    prices = []
    for item in items:
        raw_price = item.get("price") or item.get("lastQuantity")
        try:
            price = float(raw_price)
            if price > 0:
                prices.append(price)
        except (TypeError, ValueError):
            continue
    if not prices:
        raise ValueError("No valid Bybit P2P USDT/NGN advertisements")
    prices.sort()
    mid = len(prices) // 2
    median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
    return {"NGN": median}, "Bybit P2P (USDT/NGN median)"


async def _fetch_from_secondary(client: httpx.AsyncClient) -> tuple[dict[str, float], str]:
    """Fetch from jsdelivr Open Currency API mirror."""
    resp = await client.get(PROVIDER_SECONDARY, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()

    rates_raw = data.get("usd", {})
    rates = {}
    for code in TRACKED_CURRENCIES:
        val = rates_raw.get(code.lower())
        if val is not None:
            rates[code] = float(val)

    if "NGN" not in rates:
        raise ValueError("NGN rate missing from Open Currency API")

    return rates, "Open Currency API"


async def _fetch_from_tertiary(client: httpx.AsyncClient) -> tuple[dict[str, float], str]:
    """Fetch from CoinGecko Tether (digital USD) live rates."""
    resp = await client.get(PROVIDER_TERTIARY, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()

    tether_rates = data.get("tether", {})
    rates = {}
    for code in TRACKED_CURRENCIES:
        val = tether_rates.get(code.lower())
        if val is not None:
            rates[code] = float(val)

    if "NGN" not in rates:
        raise ValueError("NGN rate missing from CoinGecko Tether")

    return rates, "CoinGecko (USDT)"


async def fetch_live_forex_rates() -> tuple[dict[str, float], str]:
    """Fetch live rates from trusted providers in order of preference."""
    async with httpx.AsyncClient() as client:
        # 1. Primary: the market rate actually used by local P2P buyers.
        try:
            rates, source = await _fetch_from_bybit(client)
            logger.info("Forex rates fetched from %s: 1 USDT = ₦%.2f", source, rates["NGN"])
            return rates, source
        except Exception as exc:
            logger.warning("Bybit P2P provider failed (%s), trying ExchangeRate-API…", exc)

        # 2. Secondary
        try:
            rates, source = await _fetch_from_primary(client)
            logger.info("Forex rates fetched from %s: 1 USD = ₦%.2f", source, rates.get("NGN", 0))
            return rates, source
        except Exception as exc:
            logger.warning("Primary forex provider failed (%s), trying secondary…", exc)

        # 3. Tertiary
        try:
            rates, source = await _fetch_from_secondary(client)
            logger.info("Forex rates fetched from %s: 1 USD = ₦%.2f", source, rates.get("NGN", 0))
            return rates, source
        except Exception as exc:
            logger.warning("Secondary forex provider failed (%s), trying tertiary…", exc)

        # 4. Last provider
        try:
            rates, source = await _fetch_from_tertiary(client)
            logger.info("Forex rates fetched from %s: 1 USD = ₦%.2f", source, rates.get("NGN", 0))
            return rates, source
        except Exception as exc:
            logger.error("All forex providers failed: %s", exc)
            raise


# ── Configuration & State Helpers ────────────────────────────

async def get_or_create_config() -> ExchangeRateConfig:
    """Retrieve or initialize the exchange rate config."""
    async with async_session() as session:
        result = await session.execute(
            select(ExchangeRateConfig).where(ExchangeRateConfig.id == 1)
        )
        config = result.scalar_one_or_none()
        if config is None:
            config = ExchangeRateConfig(
                id=1,
                is_auto=True,
                fx_buffer_percent=0.0,
                manual_rate_ngn=float(settings.usd_to_ngn_rate),
            )
            session.add(config)
            await session.commit()
            await session.refresh(config)
        return config


def _apply_rates_to_memory(rates: dict[str, float], ngn_rate: float) -> None:
    """Update settings and CURRENCIES in-memory data structures."""
    # 1. Update settings.usd_to_ngn_rate
    settings.usd_to_ngn_rate = round(ngn_rate, 2)

    # 2. Update currency service mapping
    from bot.services.currency import CURRENCIES
    if "NGN" in CURRENCIES:
        CURRENCIES["NGN"].rate_per_usd = round(ngn_rate, 2)
    for code, val in rates.items():
        if code in CURRENCIES:
            CURRENCIES[code].rate_per_usd = round(val, 2)

    logger.info("Applied active rates in memory: USD->NGN = ₦%.2f", settings.usd_to_ngn_rate)


# ── Synchronization Logic ────────────────────────────────────

async def sync_forex_rates(force: bool = False) -> dict[str, Any]:
    """Synchronize exchange rates from live providers to DB and memory.

    Respects manual mode unless force=True.
    Returns status summary.
    """
    config = await get_or_create_config()

    # If in manual mode and not forced, use manual rate
    if not config.is_auto and not force:
        manual_rate = config.manual_rate_ngn or float(settings.usd_to_ngn_rate)
        _apply_rates_to_memory({}, manual_rate)
        return {
            "success": True,
            "mode": "manual",
            "rate_ngn": manual_rate,
            "source": "Manual Override",
            "last_sync": config.last_sync_at,
        }

    try:
        raw_rates, source = await fetch_live_forex_rates()
    except Exception as exc:
        # Fall back to DB saved rate if available
        async with async_session() as session:
            result = await session.execute(
                select(ExchangeRate).where(ExchangeRate.target_currency == "NGN")
            )
            saved = result.scalar_one_or_none()
            if saved:
                _apply_rates_to_memory({}, saved.rate)
                return {
                    "success": False,
                    "error": str(exc),
                    "mode": "db_fallback",
                    "rate_ngn": saved.rate,
                    "source": f"{saved.source} (cached)",
                    "last_sync": saved.updated_at,
                }

        # Otherwise keep .env default
        return {
            "success": False,
            "error": str(exc),
            "mode": "env_fallback",
            "rate_ngn": float(settings.usd_to_ngn_rate),
            "source": "Environment Default",
            "last_sync": None,
        }

    # Calculate effective NGN rate with optional FX buffer
    raw_ngn = raw_rates.get("NGN", float(settings.usd_to_ngn_rate))
    buffer_multiplier = 1.0 + (config.fx_buffer_percent / 100.0)
    effective_ngn = round(raw_ngn * buffer_multiplier, 2)

    now = datetime.now(timezone.utc)

    # Persist in DB
    async with async_session() as session:
        # Update rates table
        for code, rate_val in raw_rates.items():
            result = await session.execute(
                select(ExchangeRate).where(ExchangeRate.target_currency == code)
            )
            rec = result.scalar_one_or_none()
            val_to_save = effective_ngn if code == "NGN" else rate_val
            if rec:
                rec.rate = val_to_save
                rec.source = source
                rec.updated_at = now
            else:
                session.add(
                    ExchangeRate(
                        base_currency="USD",
                        target_currency=code,
                        rate=val_to_save,
                        source=source,
                        updated_at=now,
                    )
                )

        # Update config record
        conf_result = await session.execute(
            select(ExchangeRateConfig).where(ExchangeRateConfig.id == 1)
        )
        conf = conf_result.scalar_one()
        conf.last_sync_at = now
        conf.last_sync_source = source
        await session.commit()

    # Apply to memory
    _apply_rates_to_memory(raw_rates, effective_ngn)

    logger.info(
        "Forex sync completed ✓ (Source: %s, Raw: ₦%.2f, Buffer: +%.1f%%, Active: ₦%.2f)",
        source, raw_ngn, config.fx_buffer_percent, effective_ngn,
    )

    return {
        "success": True,
        "mode": "auto",
        "rate_ngn": effective_ngn,
        "raw_rate_ngn": raw_ngn,
        "buffer_percent": config.fx_buffer_percent,
        "source": source,
        "last_sync": now,
        "all_rates": raw_rates,
    }


async def get_forex_summary() -> dict[str, Any]:
    """Get current active forex status, rates, and configuration."""
    config = await get_or_create_config()

    async with async_session() as session:
        result = await session.execute(select(ExchangeRate))
        rates_db = {r.target_currency: r.rate for r in result.scalars().all()}

    return {
        "active_rate_ngn": float(settings.usd_to_ngn_rate),
        "is_auto": config.is_auto,
        "buffer_percent": config.fx_buffer_percent,
        "manual_rate_ngn": config.manual_rate_ngn,
        "last_sync_at": config.last_sync_at,
        "last_sync_source": config.last_sync_source or "ExchangeRate-API",
        "tracked_rates": rates_db,
    }


async def set_forex_mode(is_auto: bool, manual_rate: float | None = None) -> None:
    """Toggle between automatic daily sync and manual fixed rate."""
    async with async_session() as session:
        result = await session.execute(
            select(ExchangeRateConfig).where(ExchangeRateConfig.id == 1)
        )
        conf = result.scalar_one()
        conf.is_auto = is_auto
        if manual_rate is not None:
            conf.manual_rate_ngn = manual_rate
        await session.commit()

    if not is_auto and manual_rate is not None:
        _apply_rates_to_memory({}, manual_rate)
    elif is_auto:
        await sync_forex_rates(force=True)


async def set_fx_buffer(buffer_percent: float) -> None:
    """Set the FX buffer percentage (e.g. 0.0, 2.0, 5.0) and recalculate."""
    async with async_session() as session:
        result = await session.execute(
            select(ExchangeRateConfig).where(ExchangeRateConfig.id == 1)
        )
        conf = result.scalar_one()
        conf.fx_buffer_percent = buffer_percent
        await session.commit()

    # Re-sync to apply the new buffer
    await sync_forex_rates(force=True)


# ── Background Scheduler & Lifecycle ─────────────────────────

async def _forex_scheduler() -> None:
    """Background loop that updates rates every 12 hours."""
    while True:
        try:
            await asyncio.sleep(FOREX_SYNC_INTERVAL_SECONDS)
            logger.info("Executing scheduled daily forex sync …")
            await sync_forex_rates(force=False)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error during scheduled forex sync: %s", exc)
            await asyncio.sleep(300)  # Retry in 5 minutes on unexpected failure


async def init_forex(app=None) -> None:
    """Initialise forex service on bot startup."""
    global _forex_task

    logger.info("Initialising Forex service …")

    # 1. Load saved rates from DB first for fast startup
    try:
        async with async_session() as session:
            result = await session.execute(
                select(ExchangeRate).where(ExchangeRate.target_currency == "NGN")
            )
            saved = result.scalar_one_or_none()
            if saved and saved.rate > 0:
                settings.usd_to_ngn_rate = saved.rate
                logger.info("Loaded cached USD->NGN rate: ₦%.2f (%s)", saved.rate, saved.source)
    except Exception as exc:
        logger.warning("Could not read cached forex rates: %s", exc)

    # 2. Run initial live sync in background
    asyncio.create_task(sync_forex_rates(force=False))

    # 3. Start recurring scheduler
    if _forex_task is None or _forex_task.done():
        _forex_task = asyncio.create_task(_forex_scheduler())
        logger.info("Daily Forex scheduler started (12h interval) ✓")


async def stop_forex() -> None:
    """Cancel the background scheduler upon shutdown."""
    global _forex_task
    if _forex_task and not _forex_task.done():
        _forex_task.cancel()
        try:
            await _forex_task
        except asyncio.CancelledError:
            pass
        _forex_task = None
        logger.info("Forex scheduler stopped ✓")
