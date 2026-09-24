"""
api/routes/credits.py — Vision Credits API.

GET /credits/balance — zůstatek + posledních 20 pohybů.
Při prvním dotazu uživatele provede onboarding grant (GDD 23.12).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.routes.auth import get_current_user_id
from db import credits, pg

router = APIRouter(prefix="/credits", tags=["credits"])


class CreditBalanceResponse(BaseModel):
    balance: int
    recent_entries: list[dict[str, Any]]


@router.get("/balance", response_model=CreditBalanceResponse)
async def get_credit_balance(
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> CreditBalanceResponse:
    pool = await pg.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await credits.lock_user_credits(conn, user_id)
            await credits.ensure_onboarding_grant(conn, user_id)
            balance = await credits.get_balance(conn, user_id)
        rows = await conn.fetch(
            """
            SELECT delta, balance_after, bucket_type, origin, created_at
            FROM vision_credit_ledger
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 20
            """,
            user_id,
        )
    return CreditBalanceResponse(
        balance=balance,
        recent_entries=[
            {
                "delta":         r["delta"],
                "balance_after": r["balance_after"],
                "bucket_type":   r["bucket_type"],
                "origin":        r["origin"],
                "created_at":    r["created_at"].isoformat(),
            }
            for r in rows
        ],
    )
