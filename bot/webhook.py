"""
DRSub – Paystack webhook server.

Receives payment confirmations from Paystack and credits user wallets.
Runs as a FastAPI app alongside the Telegram bot.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import FastAPI, Request, HTTPException
from sqlalchemy import select, update

from bot.database import async_session
from bot.models.user import User
from bot.models.wallet import Wallet, Transaction, TransactionType, TransactionStatus
from bot.services.paystack import paystack, PaymentEvent

logger = logging.getLogger(__name__)

# ── FastAPI app ──────────────────────────────────────────────
webhook_app = FastAPI(title="DRSub Webhooks", docs_url=None, redoc_url=None)


@webhook_app.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {"status": "ok", "service": "drsub-webhooks"}


@webhook_app.post("/webhooks/paystack")
async def paystack_webhook(request: Request):
    """Receive and process Paystack webhook events.

    Paystack sends a POST with JSON body and signs it with
    HMAC SHA-512 in the 'x-paystack-signature' header.
    """
    # Read raw body for signature verification
    body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")
    logger.info(
        "Paystack webhook received: bytes=%d signature_present=%s",
        len(body), bool(signature),
    )

    # Verify signature
    if not signature or not paystack.verify_webhook_signature(body, signature):
        logger.warning("Invalid Paystack webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Parse JSON
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = data.get("event", "")
    event_data = data.get("data") or {}
    logger.info(
        "Paystack webhook parsed: event=%s ref=%s amount_kobo=%s",
        event_type,
        event_data.get("reference", "unknown"),
        event_data.get("amount", "unknown"),
    )

    # We only care about successful charges
    if event_type == "charge.success":
        payment = paystack.parse_charge_success(data)
        if payment:
            await _handle_charge_success(payment)
        else:
            logger.warning("Paystack charge.success payload could not be parsed")

    # Always return 200 to acknowledge receipt
    return {"status": "received"}


# ── Payment processing ───────────────────────────────────────

# Store bot application reference for sending notifications
_bot_app = None


def set_bot_app(app):
    """Store reference to the Telegram bot application for sending messages."""
    global _bot_app
    _bot_app = app


async def _handle_charge_success(payment: PaymentEvent) -> None:
    """Process a successful payment — route to wallet deposit or direct order."""
    logger.info(
        "Processing payment: ref=%s amount=₦%.2f type=%s",
        payment.reference,
        payment.amount_ngn,
        payment.metadata.get("type", "unknown"),
    )

    # Route based on metadata type
    payment_type = payment.metadata.get("type", "wallet_deposit")

    if payment_type == "direct_order":
        await _handle_direct_order_payment(payment)
    else:
        await _handle_wallet_deposit(payment)


async def _handle_wallet_deposit(payment: PaymentEvent) -> None:
    """Credit user wallet on successful deposit payment."""
    async with async_session() as session:
        # Find user by email pattern
        user = await _find_user_by_payment(session, payment)
        if not user:
            return

        # Update pending transaction to completed (if exists)
        existing = await session.execute(
            select(Transaction).where(Transaction.reference == payment.reference)
        )
        existing_tx = existing.scalar_one_or_none()

        if existing_tx:
            claim = await session.execute(
                update(Transaction)
                .where(
                    Transaction.id == existing_tx.id,
                    Transaction.status == TransactionStatus.PENDING.value,
                )
                .values(status=TransactionStatus.COMPLETED.value)
            )
            if claim.rowcount != 1:
                logger.info("Duplicate payment reference %s, skipping", payment.reference)
                return
        else:
            # Create new transaction
            amount = Decimal(str(payment.amount_ngn))
            tx = Transaction(
                user_id=user.id,
                type=TransactionType.DEPOSIT.value,
                amount=amount,
                status=TransactionStatus.COMPLETED.value,
                reference=payment.reference,
                description=f"Deposit ₦{amount:,.2f}",
            )
            session.add(tx)

        amount = Decimal(str(payment.amount_ngn))

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

        await session.commit()

        logger.info(
            "Wallet credited: user_id=%d amount=₦%.2f new_balance=₦%.2f",
            user.id, amount, wallet.balance,
        )

        # Notify user
        if _bot_app:
            try:
                await _bot_app.bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        f"✅ *Payment Received!*\n\n"
                        f"💰 Amount: *₦{amount:,.2f}*\n"
                        f"💳 New Balance: *₦{wallet.balance:,.2f}*\n"
                        f"🔖 Reference: `{payment.reference}`\n\n"
                        "You can now use your balance to purchase subscriptions! 🛒"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as exc:
                logger.error("Failed to send deposit notification: %s", exc)


async def _handle_direct_order_payment(payment: PaymentEvent) -> None:
    """Fulfill a direct order after payment is confirmed."""
    from bot.services.subexpress import subexpress, SubExpressError
    from bot.models.order import Order, OrderStatus

    meta = payment.metadata
    product_id = int(meta.get("product_id", 0))
    product_name = str(meta.get("product_name", "Unknown"))
    quantity = int(meta.get("quantity", 1))
    telegram_id = int(meta.get("telegram_id", 0))
    user_id = int(meta.get("user_id", 0))
    amount_ngn = Decimal(str(payment.amount_ngn))
    amount_usd = Decimal(str(meta.get("amount_usd", 0)))

    async with async_session() as session:
        # Claim the pending payment atomically. The poller and webhook may
        # observe the same Paystack event at the same time.
        existing = await session.execute(
            select(Transaction).where(Transaction.reference == payment.reference)
        )
        existing_tx = existing.scalar_one_or_none()
        if not existing_tx:
            logger.warning("Direct payment has no pending transaction: ref=%s", payment.reference)
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
            logger.info("Duplicate direct order payment %s, skipping", payment.reference)
            return

        # Create order record
        order = Order(
            user_id=user_id,
            transaction_id=existing_tx.id,
            product_id=product_id,
            product_name=product_name,
            quantity=quantity,
            amount_ngn=amount_ngn,
            amount_usd=amount_usd,
            status=OrderStatus.PAID.value,
        )
        session.add(order)
        await session.flush()
        await session.commit()

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

        # Deliver keys to user
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

        logger.info("Direct order fulfilled: order_id=%d product=%s", order.id, product_name)

    except SubExpressError as exc:
        logger.error("SubExpress order failed for direct payment: %s", exc)

        # Mark order as failed — admin will need to manually handle refund
        async with async_session() as session:
            order_result = await session.execute(
                select(Order).where(Order.id == order.id)
            )
            db_order = order_result.scalar_one()
            db_order.status = OrderStatus.FAILED.value
            db_order.error_message = str(exc)
            await session.commit()

        if _bot_app and telegram_id:
            try:
                await _bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        f"❌ *Order Failed*\n\n"
                        f"{exc.user_message}\n\n"
                        f"Your payment of *₦{amount_ngn:,.2f}* has been received.\n"
                        "Please contact admin for a refund or retry."
                    ),
                    parse_mode="Markdown",
                )
            except Exception as send_exc:
                logger.error("Failed to send failure notification: %s", send_exc)


async def _find_user_by_payment(session, payment: PaymentEvent):
    """Find a user from payment metadata or email."""
    # Try metadata first
    telegram_id = payment.metadata.get("telegram_id")
    if telegram_id:
        result = await session.execute(
            select(User).where(User.telegram_id == int(telegram_id))
        )
        user = result.scalar_one_or_none()
        if user:
            return user

    # Try email pattern
    email = payment.customer_email
    if email.startswith("tg_") and email.endswith("@drsub.bot"):
        try:
            tg_id = int(email.split("_")[1].split("@")[0])
            result = await session.execute(
                select(User).where(User.telegram_id == tg_id)
            )
            return result.scalar_one_or_none()
        except (ValueError, IndexError):
            pass

    # Try customer code
    if payment.customer_code:
        result = await session.execute(
            select(User).where(User.paystack_customer_code == payment.customer_code)
        )
        return result.scalar_one_or_none()

    logger.error("Could not find user for payment: ref=%s", payment.reference)
    return None
