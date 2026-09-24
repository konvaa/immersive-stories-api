"""
tests/test_db_contracts.py — Unit testy SQL kontraktů db/pg.py (audit K2, K4).

Bez živé DB: FakeConn zachytává query + argumenty. Testuje se KONTRAKT
(scoped idempotency, stabilní řazení), ne Postgres samotný.
"""

from __future__ import annotations

import asyncio

import pytest

from db import pg

pytestmark = pytest.mark.unit


class FakeConn:
    """Zachytává asyncpg volání; vrací prázdné výsledky."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args))
        return None

    async def fetch(self, query: str, *args):
        self.calls.append((query, args))
        return []


def _q(conn: FakeConn, substr: str) -> str:
    """Najde zachycenou query obsahující substring."""
    for q, _ in conn.calls:
        if substr in q:
            return q
    raise AssertionError(f"Query s '{substr}' nebyla volána: {conn.calls}")


# ---------------------------------------------------------------------------
# K2: idempotency lookup scoped na (campaign_id, request_id)
# ---------------------------------------------------------------------------

def test_find_event_scoped_by_campaign():
    conn = FakeConn()
    asyncio.run(pg.find_event_by_request_id(conn, "camp-1", "req-1"))
    query, args = conn.calls[0]
    assert "campaign_id = $1" in query and "request_id = $2" in query, (
        f"Idempotency lookup není scoped na kampaň: {query}"
    )
    assert args == ("camp-1", "req-1")


def test_find_event_requires_campaign_id():
    """Signatura nesmí umožnit lookup jen přes request_id (regrese K2)."""
    import inspect
    params = list(inspect.signature(pg.find_event_by_request_id).parameters)
    assert params == ["conn", "campaign_id", "request_id"]


# ---------------------------------------------------------------------------
# K4: stabilní řazení NPC a lokací ve world snapshotu
# ---------------------------------------------------------------------------

def test_world_view_npcs_ordered():
    conn = FakeConn()
    asyncio.run(pg.build_frozen_world_view(conn, "camp-1", {"seed": 1}))
    npc_q = _q(conn, "FROM npcs")
    assert "ORDER BY created_at, id" in npc_q, (
        f"NPC fetch bez stabilního řazení — cíl ATTACKu/iniciativa "
        f"by závisely na fyzickém pořadí řádků: {npc_q}"
    )


def test_world_view_locations_ordered():
    conn = FakeConn()
    asyncio.run(pg.build_frozen_world_view(conn, "camp-1", {"seed": 1}))
    loc_q = _q(conn, "FROM locations")
    assert "ORDER BY created_at, id" in loc_q
