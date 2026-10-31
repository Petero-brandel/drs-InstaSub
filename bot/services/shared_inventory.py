"""Transactional shared-account reuse for selected provider products."""

from __future__ import annotations

from sqlalchemy import select

from bot.database import async_session
from bot.models.reuse_config import ReuseConfig
from bot.models.shared_account import SharedAccount


async def get_reuse_config(product_id: int, source: str = "subexpress") -> ReuseConfig | None:
    async with async_session() as session:
        return await session.scalar(select(ReuseConfig).where(
            ReuseConfig.product_id == product_id,
            ReuseConfig.source == source,
        ))


async def save_reuse_config(
    product_id: int,
    source: str,
    product_name: str,
    max_reuses: int,
) -> ReuseConfig:
    async with async_session() as session:
        config = await session.scalar(select(ReuseConfig).where(
            ReuseConfig.product_id == product_id,
            ReuseConfig.source == source,
        ))
        if config is None:
            config = ReuseConfig(
                product_id=product_id,
                source=source,
                product_name=product_name,
                enabled=max_reuses > 0,
                max_reuses=max_reuses,
            )
            session.add(config)
        else:
            config.product_name = product_name
            config.max_reuses = max_reuses
            config.enabled = max_reuses > 0
            accounts_result = await session.execute(select(SharedAccount).where(
                SharedAccount.product_id == product_id,
                SharedAccount.source == source,
                SharedAccount.is_active == True,  # noqa: E712
            ))
            for account in accounts_result.scalars().all():
                account.max_reuses = max_reuses
        await session.commit()
        await session.refresh(config)
        return config


async def claim_shared_account(product_id: int, source: str = "subexpress") -> SharedAccount | None:
    """Claim one available use, atomically, before provider fulfillment."""
    async with async_session() as session:
        config = await session.scalar(select(ReuseConfig).where(
            ReuseConfig.product_id == product_id,
            ReuseConfig.source == source,
        ))
        if not config or not config.enabled or config.max_reuses <= 0:
            return None

        result = await session.execute(
            select(SharedAccount)
            .where(
                SharedAccount.product_id == product_id,
                SharedAccount.source == source,
                SharedAccount.is_active == True,  # noqa: E712
                SharedAccount.assigned_count < SharedAccount.max_reuses,
            )
            .order_by(SharedAccount.id)
            .with_for_update()
            .limit(1)
        )
        account = result.scalar_one_or_none()
        if account is None:
            return None
        account.assigned_count += 1
        await session.commit()
        return account


async def store_provider_account(
    product_id: int,
    source: str,
    product_name: str,
    credentials: list | dict,
) -> SharedAccount | None:
    """Save the first provider response as a reusable account and claim its first use."""
    async with async_session() as session:
        config = await session.scalar(select(ReuseConfig).where(
            ReuseConfig.product_id == product_id,
            ReuseConfig.source == source,
        ))
        if not config or not config.enabled or config.max_reuses <= 0:
            return None
        account = SharedAccount(
            product_id=product_id,
            source=source,
            product_name=product_name,
            credentials=credentials,
            assigned_count=1,
            max_reuses=config.max_reuses,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        return account


def account_credentials(account: SharedAccount) -> list:
    if isinstance(account.credentials, list):
        return account.credentials
    if isinstance(account.credentials, dict):
        return [account.credentials]
    return [str(account.credentials)]
