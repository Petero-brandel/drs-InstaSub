"""
DRSub – Payment verification poller.

Since the bot runs locally without a public URL, we can't receive
Paystack webhooks. Instead, this module polls Paystack's verify
endpoint to check if a pending payment has been completed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update

from bot.config import settings
from bot.database import async_session
from bot.models.user import User
from bot.models.wallet import Wallet, Transaction, TransactionType, TransactionStatus
from bot.models.order import Order, OrderStatus
from bot.services.paystack import paystack, PaystackError
from bot.services.subexpress import subexpress, SubExpressError

logger = logging.getLogger(__name__)

# Store reference to the bot app for sending notifications
_bot_app = None
_cleanup_task: asyncio.Task | None = None
PENDING_TRANSACTION_TTL = timedelta(minutes=15)
PENDING_TRANSACTION_CLEANUP_INTERVAL = 60


def set_bot_app(app):
    global _bot_app
    _bot_app = app


async def expire_pending_transactions() -> int:
    """Cancel payment transactions that have been pending for 15 minutes."""
    cutoff = datetime.now(timezone.utc) - PENDING_TRANSACTION_TTL
    async with async_session() as session:
        result = await session.execute(
            update(Transaction)
            .where(
                Transaction.status == TransactionStatus.PENDING.value,
                Transaction.created_at < cutoff,
            )
            .values(status=TransactionStatus.FAILED.value)
        )
        await session.commit()

    expired_count = result.rowcount or 0
    if expired_count:
        logger.info("Expired %d pending payment transaction(s)", expired_count)
    return expired_count


async def _pending_transaction_cleanup_loop() -> None:
    """Keep pending payment transactions bounded across bot restarts."""
    while True:
        try:
            await expire_pending_transactions()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pending transaction cleanup failed")
        await asyncio.sleep(PENDING_TRANSACTION_CLEANUP_INTERVAL)


async def start_pending_transaction_cleanup() -> None:
    """Start the recurring pending transaction cleanup task."""
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        await expire_pending_transactions()
        _cleanup_task = asyncio.create_task(_pending_transaction_cleanup_loop())


async def stop_pending_transaction_cleanup() -> None:
    """Stop the recurring pending transaction cleanup task."""
    global _cleanup_task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None


async def poll_payment(
    reference: str,
    metadata: dict,
    max_attempts: int = 120,
    interval_seconds: int = 8,
) -> None:
    """Poll Paystack to verify a payment, then process it.

    Runs as a background task. Checks every `interval_seconds` for
    up to `max_attempts` (approx 16 minutes).
    """
    payment_type = metadata.get("type", "wallet_deposit")
    logger.info(
        "Payment polling started: ref=%s type=%s tg_user=%s max_attempts=%d interval=%ds",
        reference, payment_type, metadata.get("telegram_id"), max_attempts, interval_seconds,
    )

    for attempt in range(1, max_attempts + 1):
        await asyncio.sleep(interval_seconds)

        try:
            data = await paystack.verify_transaction(reference)
            status = data.get("status", "")

            if status == "success":
                logger.info("Payment verified: ref=%s (attempt %d)", reference, attempt)

                if payment_type == "direct_order":
                    await _process_direct_order(reference, data, metadata)
                else:
                    await _process_wallet_deposit(reference, data, metadata)
                return

            elif status in ("failed", "reversed"):
                logger.info("Payment %s: ref=%s", status, reference)
                # Mark transaction as failed
                async with async_session() as session:
                    result = await session.execute(
                        select(Transaction).where(Transaction.reference == reference)
                    )
                    tx = result.scalar_one_or_none()
                    if tx and tx.status == TransactionStatus.PENDING.value:
                        tx.status = TransactionStatus.FAILED.value
                        await session.commit()

                # Notify user only on actual terminal failure
                telegram_id = metadata.get("telegram_id")
                if _bot_app and telegram_id:
                    try:
                        await _bot_app.bot.send_message(
                            chat_id=int(telegram_id),
                            text=f"❌ Payment {status}.\nReference: `{reference}`",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                return

            else:
                # Still pending / in progress
                # Note: Paystack reports 'abandoned' for all newly created transactions until paid
                if attempt % 5 == 0:
                    logger.debug(
                        "Payment awaiting customer: ref=%s attempt=%d/%d status=%s",
                        reference, attempt, max_attempts, status,
                    )

        except PaystackError as exc:
            logger.warning("Verify failed for ref=%s: %s", reference, exc)
        except Exception:
            logger.exception("Unexpected payment polling error: ref=%s attempt=%d", reference, attempt)

    # Expired — payment was never completed
    logger.info("Payment polling expired: ref=%s", reference)
    async with async_session() as session:
        result = await session.execute(
            select(Transaction).where(Transaction.reference == reference)
        )
        tx = result.scalar_one_or_none()
        if tx and tx.status == TransactionStatus.PENDING.value:
            tx.status = TransactionStatus.FAILED.value
            await session.commit()


# ── Wallet deposit processing ────────────────────────────────

async def _process_wallet_deposit(reference: str, data: dict, metadata: dict) -> None:
    """Credit user wallet after confirmed payment."""
    amount_kobo = int(data.get("amount", 0))
    amount = Decimal(str(amount_kobo / 100))
    user_id = int(metadata.get("user_id", 0))
    telegram_id = int(metadata.get("telegram_id", 0))

    async with async_session() as session:
        # Claim the payment atomically so a webhook and this poller cannot
        # credit the same deposit twice.
        result = await session.execute(
            select(Transaction).where(Transaction.reference == reference)
        )
        tx = result.scalar_one_or_none()
        if not tx:
            logger.warning("Wallet deposit has no pending transaction: ref=%s", reference)
            return
        claim = await session.execute(
            update(Transaction)
            .where(
                Transaction.id == tx.id,
                Transaction.status == TransactionStatus.PENDING.value,
            )
            .values(status=TransactionStatus.COMPLETED.value)
        )
        if claim.rowcount != 1:
            logger.info("Duplicate wallet deposit rejected: ref=%s", reference)
            return

        # Credit wallet
        wallet_result = await session.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )
        wallet = wallet_result.scalar_one_or_none()

        if wallet is None:
            wallet = Wallet(user_id=user_id, balance=amount)
            session.add(wallet)
        else:
            wallet.balance += amount

        await session.commit()

        logger.info(
            "Wallet credited via polling: user_id=%d amount=₦%.2f balance=₦%.2f",
            user_id, amount, wallet.balance,
        )

        # Notify user
        if _bot_app and telegram_id:
            try:
                await _bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        f"✅ *Payment Received!*\n\n"
                        f"💰 Amount: *₦{amount:,.2f}*\n"
                        f"💳 New Balance: *₦{wallet.balance:,.2f}*\n\n"
                        "You can now purchase subscriptions! 🛒"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as exc:
                logger.error("Failed to send deposit notification: %s", exc)


# ── Direct order processing ──────────────────────────────────

async def _process_direct_order(reference: str, data: dict, metadata: dict) -> None:
    """Fulfill a direct order after payment is confirmed."""
    product_id = int(metadata.get("product_id", 0))
    product_name = str(metadata.get("product_name", "Unknown"))
    quantity = int(metadata.get("quantity", 1))
    telegram_id = int(metadata.get("telegram_id", 0))
    user_id = int(metadata.get("user_id", 0))
    amount_kobo = int(data.get("amount", 0))
    amount_ngn = Decimal(str(amount_kobo / 100))
    amount_usd = Decimal(str(metadata.get("amount_usd", 0)))
    source = str(metadata.get("source", "subexpress"))
    manual_product_id = metadata.get("manual_product_id")

    async with async_session() as session:
        # Claim the pending payment atomically. Only the worker that changes
        # pending -> completed may create and fulfill the order.
        result = await session.execute(
            select(Transaction).where(Transaction.reference == reference)
        )
        existing_tx = result.scalar_one_or_none()
        if not existing_tx:
            logger.warning("Direct payment has no pending transaction: ref=%s", reference)
            return
        claim = await session.execute(
            update(Transaction)
            .where(
                Transaction.id == existing_tx.id,
                Transaction.status == TransactionStatus.PENDING.value,
            )
            .values(status=TransactionStatus.COMPLETED.value)
        )
        if claim.rowcount != 1:
            logger.info("Duplicate direct payment claim rejected: ref=%s", reference)
            return

        # Create order
        order = Order(
            user_id=user_id,
            transaction_id=existing_tx.id,
            product_id=product_id,
            product_name=product_name,
            quantity=quantity,
            amount_ngn=amount_ngn,
            amount_usd=amount_usd,
            status=OrderStatus.PAID.value,
            source=source,
            manual_product_id=int(manual_product_id) if manual_product_id else None,
        )
        session.add(order)
        await session.flush()
        await session.commit()

    if source == "manual":
        if _bot_app:
            for admin_id in settings.admin_id_list:
                try:
                    await _bot_app.bot.send_message(
                        chat_id=admin_id,
                        text=(f"🛒 *Manual order awaiting fulfillment*\n\nOrder: `#{order.id}`\n"
                              f"Product: *{product_name}*\nUser: `{telegram_id}`\nAmount: ₦{amount_ngn:,.2f}"),
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Could not alert admin for manual order")
            try:
                await _bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=(f"✅ Payment received for *{product_name}*.\n\n"
                          "An administrator will fulfill this order manually and message you here."),
                    parse_mode="Markdown",
                )
            except Exception:
                logger.exception("Could not notify manual-order customer")
        return

    from bot.services.shared_inventory import claim_shared_account, account_credentials, store_provider_account
    super_reseller = bool(metadata.get("super_reseller", False))
    shared_account = None if super_reseller else await claim_shared_account(product_id, source)
    if shared_account:
        credentials = account_credentials(shared_account)
        async with async_session() as session:
            db_order = await session.get(Order, order.id)
            db_order.shared_account_id = shared_account.id
            db_order.keys = credentials
            db_order.status = OrderStatus.FULFILLED.value
            await session.commit()
        keys_display = "\n".join(f"`{key}`" for key in credentials)
        if _bot_app and telegram_id:
            try:
                await _bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        f"✅ *Order Complete!*\n\n*{product_name}*\n"
                        f"Paid: ₦{amount_ngn:,.2f}\n\n"
                        f"*Login details:*\n{keys_display}\n\n"
                        f"Order ID: `#{order.id}`\nPlease save these details."
                    ),
                    parse_mode="Markdown",
                )
            except Exception as exc:
                logger.error("Failed to send shared-account order: %s", exc)
        try:
            from bot.services.referral import process_referral_commission, get_referral_settings
            ref_settings = await get_referral_settings(_bot_app.bot_data if _bot_app else {})
            if ref_settings.get("enabled", True):
                await process_referral_commission(
                    order=order,
                    buyer_user_id=user_id,
                    order_amount_ngn=amount_ngn,
                    product_name=product_name,
                    bot_app=_bot_app,
                )
        except Exception as exc:
            logger.error("Shared-account referral commission error: %s", exc)
        return

    # Place SubExpress order
    try:
        result = await subexpress.place_order(product_id, quantity)

        # Update order with keys
        async with async_session() as session:
            order_result = await session.execute(
                select(Order).where(Order.id == order.id)
            )
            db_order = order_result.scalar_one()
            db_order.subexpress_order_id = result.order_id
            db_order.keys = result.items
            db_order.status = OrderStatus.FULFILLED.value
            await session.commit()

        shared_account = None
        if not super_reseller:
            shared_account = await store_provider_account(
                product_id,
                source,
                product_name,
                result.items,
            )
        if shared_account:
            async with async_session() as session:
                db_order = await session.get(Order, order.id)
                db_order.shared_account_id = shared_account.id
                await session.commit()

        # Deliver keys
        keys_display = "\n".join(f"`{key}`" for key in result.items)

        if _bot_app and telegram_id:
            try:
                await _bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        f"✅ *Order Complete!*\n\n"
                        f"🏷️ *{product_name}*\n"
                        f"📦 Quantity: {quantity}\n"
                        f"💵 Paid: ₦{amount_ngn:,.2f}\n\n"
                        f"🔑 *Your key{'s' if quantity > 1 else ''}:*\n"
                        f"{keys_display}\n\n"
                        f"📋 Order ID: `#{order.id}`\n\n"
                        "⚠️ _Save your keys! They won't be shown again._"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as exc:
                logger.error("Failed to send order notification: %s", exc)

        logger.info("Direct order fulfilled: order_id=%d", order.id)

        # Process referral commission
        try:
            from bot.services.referral import process_referral_commission, get_referral_settings
            bot_data = _bot_app.bot_data if _bot_app else {}
            ref_settings = await get_referral_settings(bot_data)
            if ref_settings.get("enabled", True):
                await process_referral_commission(
                    order=order,
                    buyer_user_id=user_id,
                    order_amount_ngn=amount_ngn,
                    product_name=product_name,
                    bot_app=_bot_app,
                    commission_rate=ref_settings.get("commission_rate"),
                )
        except Exception as ref_exc:
            logger.error("Referral commission error: %s", ref_exc)

    except SubExpressError as exc:
        logger.error("SubExpress order failed: %s", exc)

        async with async_session() as session:
            # Update order status
            order_result = await session.execute(
                select(Order).where(Order.id == order.id)
            )
            db_order = order_result.scalar_one()
            db_order.status = OrderStatus.REFUNDED.value
            db_order.error_message = str(exc)

            # Credit user's wallet so money is never lost
            wallet_result = await session.execute(
                select(Wallet).where(Wallet.user_id == user_id)
            )
            wallet = wallet_result.scalar_one_or_none()
            if wallet is None:
                wallet = Wallet(user_id=user_id, balance=amount_ngn)
                session.add(wallet)
            else:
                wallet.balance += amount_ngn

            # Record refund transaction
            refund_tx = Transaction(
                user_id=user_id,
                type=TransactionType.REFUND.value,
                amount=amount_ngn,
                status=TransactionStatus.COMPLETED.value,
                description=f"Refund: {product_name} (restocking)",
            )
            session.add(refund_tx)
            await session.commit()

        # Notify user that funds are safely in their wallet
        if _bot_app and telegram_id:
            try:
                await _bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        f"⚠️ *Order Delivery Update*\n\n"
                        f"Your payment of *₦{amount_ngn:,.2f}* was received, but automated "
                        f"fulfillment is temporarily paused while provider restocks.\n\n"
                        f"✅ *We have credited ₦{amount_ngn:,.2f} to your DRSub Wallet!*\n"
                        f"Your new wallet balance is ready to use anytime.\n\n"
                        f"Order ID: `#{order.id}`\n"
                        f"Contact support if you have questions."
                    ),
                    parse_mode="Markdown",
                )
            except Exception as user_err:
                logger.error("Failed to notify user: %s", user_err)

        # Alert the admin immediately
        if _bot_app:
            from bot.config import settings
            admin_msg = (
                f"🚨 *ADMIN ALERT: Order Fulfillment Failed*\n\n"
                f"• User ID: `{telegram_id}`\n"
                f"• Product: *{product_name}* (Qty: {quantity})\n"
                f"• Error: `{exc}`\n\n"
                f"⚠️ *Reason*: SubExpress account has insufficient balance ($0.83). "
                f"Please top up your SubExpress partner balance at subexpress.space!\n\n"
                f"ℹ️ User was automatically refunded ₦{amount_ngn:,.2f} to their bot wallet."
            )
            for adm_id in settings.admin_id_list:
                try:
                    await _bot_app.bot.send_message(
                        chat_id=adm_id,
                        text=admin_msg,
                        parse_mode="Markdown",
                    )
                except Exception as adm_err:
                    logger.error("Failed to alert admin %d: %s", adm_id, adm_err)
