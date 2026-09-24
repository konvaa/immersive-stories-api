"""
db/credits.py — Vision Credits ledger operace (GDD Addendum G/H, 23.18, 23.26).

Všechny operace běží přes asyncpg UVNITŘ transakce volajícího a pod
advisory lockem uživatele — žádný pohyb kreditů mimo ledger, žádné race
conditions při souběžných požadavcích.

Vzor použití:
    async with conn.transaction():
        await lock_user_credits(conn, user_id)
        await ensure_onboarding_grant(conn, user_id)
        balance = await charge(conn, user_id, cost, reference_id=req_id)
"""

from __future__ import annotations

import os

import asyncpg


class InsufficientCreditsError(Exception):
    """Uživatel nemá dost kreditů na požadovanou útratu."""

    def __init__(self, balance: int, cost: int):
        self.balance = balance
        self.cost = cost
        super().__init__(f"balance={balance}, cost={cost}")


def onboarding_credits() -> int:
    return int(os.getenv("VISION_FREE_ONBOARDING_CREDITS", "3"))


async def lock_user_credits(conn: asyncpg.Connection, user_id: str) -> None:
    """Transakční advisory lock na kreditní účet uživatele."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('credits:' || $1::text, 0))",
        user_id,
    )


async def get_balance(conn: asyncpg.Connection, user_id: str) -> int:
    row = await conn.fetchrow(
        "SELECT COALESCE(SUM(delta), 0) AS balance "
        "FROM vision_credit_ledger WHERE user_id = $1",
        user_id,
    )
    return int(row["balance"])


async def ensure_onboarding_grant(conn: asyncpg.Connection, user_id: str) -> None:
    """
    Lazy onboarding grant (GDD 23.12): při prvním kontaktu s kreditním
    systémem dostane uživatel free kredity. Idempotentní — kontroluje
    existenci jakéhokoli ledger záznamu uživatele. Volat pod lockem.
    """
    exists = await conn.fetchval(
        "SELECT 1 FROM vision_credit_ledger WHERE user_id = $1 LIMIT 1",
        user_id,
    )
    if exists:
        return
    amount = onboarding_credits()
    if amount <= 0:
        return
    await conn.execute(
        """
        INSERT INTO vision_credit_ledger
            (user_id, delta, balance_after, bucket_type, origin)
        VALUES ($1, $2, $2, 'free', 'onboarding')
        """,
        user_id, amount,
    )


async def _append(
    conn: asyncpg.Connection,
    user_id: str,
    delta: int,
    origin: str,
    bucket_type: str = "free",
    reference_id: str | None = None,
) -> int:
    """Zapíše ledger záznam a vrátí nový zůstatek. Volat pod lockem."""
    balance = await get_balance(conn, user_id)
    new_balance = balance + delta
    if new_balance < 0:
        raise InsufficientCreditsError(balance=balance, cost=-delta)
    await conn.execute(
        """
        INSERT INTO vision_credit_ledger
            (user_id, delta, balance_after, bucket_type, origin, reference_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        user_id, delta, new_balance, bucket_type, origin, reference_id,
    )
    return new_balance


async def charge(
    conn: asyncpg.Connection,
    user_id: str,
    cost: int,
    reference_id: str | None = None,
) -> int:
    """Strhne kredity (origin='spend'). Vrátí nový zůstatek.

    Raises:
        InsufficientCreditsError pokud zůstatek nestačí.
    """
    if cost <= 0:
        return await get_balance(conn, user_id)
    return await _append(conn, user_id, -cost, "spend", reference_id=reference_id)


async def refund(
    conn: asyncpg.Connection,
    user_id: str,
    amount: int,
    reference_id: str | None = None,
) -> int:
    """Vrátí kredity po technickém selhání (origin='refund')."""
    if amount <= 0:
        return await get_balance(conn, user_id)
    return await _append(conn, user_id, amount, "refund", reference_id=reference_id)
