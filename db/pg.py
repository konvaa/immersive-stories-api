"""
db/pg.py — Přímé Postgres připojení (asyncpg) pro transakční /action flow.

Dle GDD 20.1 musí /action běžet v JEDNÉ transakci:
  BEGIN → pg_advisory_xact_lock → idempotency → load → resolve → write → COMMIT

Supabase REST klient transakce neumí, proto /action používá asyncpg.
Ostatní endpointy (campaign, narrative, visualize) zůstávají na REST klientu.

Env:
  DATABASE_URL — Supabase Postgres connection string.
                 Doporučeno: Supavisor session mode (port 5432),
                 např. postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:5432/postgres

Pozn.: statement_cache_size=0 kvůli kompatibilitě se Supavisor poolerem
(prepared statements nejsou v transaction/session poolingu spolehlivé).
"""

from __future__ import annotations

import json
import os

import asyncpg

from db.writer import build_event_payload
from engine.effect_applier import AppliedState
from models.state import (
    Effect,
    FrozenWorldView,
    LocationSnapshot,
    NPCSnapshot,
    PlayerSnapshot,
)

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """json/jsonb sloupce ↔ Python dict/list automaticky."""
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(
            typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


async def get_pool() -> asyncpg.Pool:
    """Lazy singleton pool. Vyžaduje env DATABASE_URL."""
    global _pool
    if _pool is None:
        dsn = os.environ["DATABASE_URL"]
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=5,
            statement_cache_size=0,
            init=_init_connection,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------

async def acquire_campaign_lock(conn: asyncpg.Connection, campaign_id: str) -> None:
    """
    Transakčně-scopovaný advisory lock na kampaň (GDD 20.1 krok 4).
    Uvolní se automaticky při COMMIT/ROLLBACK. Volat POUZE uvnitř transakce.
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
        campaign_id,
    )


# ---------------------------------------------------------------------------
# Read path (ekvivalent db/loader.py, ale přes asyncpg)
# ---------------------------------------------------------------------------

async def load_campaign_row(conn: asyncpg.Connection, campaign_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT * FROM campaigns WHERE id = $1", campaign_id
    )
    return dict(row) if row else None


async def find_event_by_request_id(
    conn: asyncpg.Connection, campaign_id: str, request_id: str
) -> dict | None:
    """
    Idempotency check (GDD 20.1 krok 5) — uvnitř locku.

    Lookup MUSÍ být scoped na kampaň (audit K2): events mají
    UNIQUE(campaign_id, request_id), takže stejné request_id může legálně
    existovat v jiné kampani — lookup jen přes request_id by vrátil cizí event.
    """
    row = await conn.fetchrow(
        "SELECT id, action_type, outcome, effects, tick, stakes, mitigated "
        "FROM events WHERE campaign_id = $1 AND request_id = $2 LIMIT 1",
        campaign_id, request_id,
    )
    return dict(row) if row else None


async def load_narrative_for_event(
    conn: asyncpg.Connection, event_id: str
) -> str | None:
    row = await conn.fetchrow(
        "SELECT narrative FROM event_narratives WHERE event_id = $1 LIMIT 1",
        event_id,
    )
    return row["narrative"] if row else None


async def build_frozen_world_view(
    conn: asyncpg.Connection, campaign_id: str, campaign_row: dict
) -> FrozenWorldView:
    """Stejné mapování jako db/loader.build_frozen_world_view (REST verze)."""
    player_state: dict = campaign_row.get("player_state") or {}
    player_state.setdefault("player_id", str(campaign_row.get("user_id", "")))
    player_state.setdefault("campaign_id", campaign_id)
    player_state.setdefault("location_id", "crossroads_inn")
    player = PlayerSnapshot(**player_state)

    # ORDER BY (audit K4): pořadí NPC určuje cíl ATTACKu a pořadí iniciativy —
    # bez explicitního řazení je „první NPC" implementation detail Postgresu
    # a determinismus světa (GDD 20.4) by stál na náhodě.
    npc_rows = await conn.fetch(
        "SELECT * FROM npcs WHERE campaign_id = $1 ORDER BY created_at, id",
        campaign_id,
    )
    npcs = []
    for row in npc_rows:
        state_json: dict = row["state_json"] or {}
        npcs.append(NPCSnapshot(
            npc_id      = str(row["id"]),
            campaign_id = campaign_id,
            template_id = row["template_id"] or "",
            name        = row["name"] or "",
            location_id = row["location_id"] or "",
            trust       = row["trust"] if row["trust"] is not None else 50,
            fear        = row["fear"] or 0,
            suspicion   = row["suspicion"] or 0,
            hp          = row["hp"] if row["hp"] is not None else 100,
            disposition = state_json.get("disposition", "neutral"),
            flags       = state_json.get("flags", []),
            knowledge   = state_json.get("knowledge", []),
        ))

    loc_rows = await conn.fetch(
        "SELECT * FROM locations WHERE campaign_id = $1 ORDER BY created_at, id",
        campaign_id,
    )
    locations = []
    for row in loc_rows:
        d = dict(row)
        locations.append(LocationSnapshot(
            row_id      = str(d["id"]),
            location_id = d.get("location_id") or str(d["id"]),
            campaign_id = campaign_id,
            name        = d.get("name") or "",
            description = d.get("description") or "",
            known_exits = list(d.get("known_exits") or []),
            hidden_exits= list(d.get("hidden_exits") or []),
            flags       = d.get("flags") or [],
        ))

    return FrozenWorldView(
        campaign_id     = campaign_id,
        engine_version  = campaign_row.get("engine_version") or "1.0.0",
        ruleset_version = campaign_row.get("ruleset_version") or "1.0.0",
        seed            = campaign_row.get("seed") or 0,
        tick            = campaign_row.get("tick") or 0,
        world_pressure  = campaign_row.get("world_pressure") or 0,
        time_of_day     = campaign_row.get("time_of_day") or "morning",
        difficulty      = campaign_row.get("difficulty") or "normal",
        player          = player,
        npcs            = tuple(npcs),
        locations       = tuple(locations),
    )


# ---------------------------------------------------------------------------
# Write path (ekvivalent db/writer.py, ale přes asyncpg uvnitř transakce)
# ---------------------------------------------------------------------------

async def insert_event(
    conn: asyncpg.Connection,
    campaign_id: str,
    request_id: str,
    tick: int,
    action_type: str,
    intent: str,
    outcome: str,
    effects: list[Effect],
    world: FrozenWorldView,
    roll: int = 10,
    dc: int = 10,
    stakes: str = "MEDIUM",
    mitigated: bool = False,
) -> dict:
    """INSERT INTO events; payload sdílený s REST verzí (build_event_payload)."""
    payload = build_event_payload(
        campaign_id=campaign_id, request_id=request_id, tick=tick,
        action_type=action_type, intent=intent, outcome=outcome,
        effects=effects, world=world, roll=roll, dc=dc, stakes=stakes,
        mitigated=mitigated,
    )
    row = await conn.fetchrow(
        """
        INSERT INTO events (
            campaign_id, request_id, tick, engine_version, actor,
            action_type, intent_type, raw_intent, normalized_intent,
            context_signature, rng_seed, roll, dc, stakes, outcome,
            effects, causal_tags, mitigated
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
        RETURNING *
        """,
        payload["campaign_id"], payload["request_id"], payload["tick"],
        payload["engine_version"], payload["actor"], payload["action_type"],
        payload["intent_type"], payload["raw_intent"], payload["normalized_intent"],
        payload["context_signature"], payload["rng_seed"], payload["roll"],
        payload["dc"], payload["stakes"], payload["outcome"],
        payload["effects"], payload["causal_tags"], payload["mitigated"],
    )
    if row is None:
        raise RuntimeError("INSERT INTO events selhal")
    return dict(row)


async def apply_applied_state(
    conn: asyncpg.Connection, campaign_id: str, state: AppliedState
) -> None:
    await conn.execute(
        """
        UPDATE campaigns SET
            player_state = $2, tick = $3, world_pressure = $4,
            time_of_day = $5, last_played_at = now()
        WHERE id = $1
        """,
        campaign_id, state.player_state, state.tick,
        state.world_pressure, state.time_of_day,
    )
    for delta in state.npc_deltas:
        await conn.execute(
            """
            UPDATE npcs SET trust=$2, fear=$3, suspicion=$4, hp=$5, state_json=$6
            WHERE id = $1
            """,
            delta.npc_id, delta.trust, delta.fear,
            delta.suspicion, delta.hp, delta.state_json,
        )
    for loc in state.location_flag_updates:
        await conn.execute(
            "UPDATE locations SET flags = $2 WHERE id = $1",
            loc["row_id"], loc["flags"],
        )


async def upsert_campaign_snapshot(
    conn: asyncpg.Connection,
    campaign_id: str,
    state: AppliedState,
    effects: list[Effect],
) -> None:
    snapshot_json = {
        "player_state":   state.player_state,
        "world_pressure": state.world_pressure,
        "time_of_day":    state.time_of_day,
        "global_flags":   [],
        "last_effects":   [e.model_dump() for e in effects],
    }
    await conn.execute(
        """
        INSERT INTO campaign_snapshots (campaign_id, tick, snapshot_json, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (campaign_id)
        DO UPDATE SET tick = $2, snapshot_json = $3, updated_at = now()
        """,
        campaign_id, state.tick, snapshot_json,
    )


async def insert_event_narrative(
    conn: asyncpg.Connection,
    event_id: str,
    campaign_id: str,
    narrative: str,
    context_signature: str,
    model: str,
    prompt_version: str = "1.0.0",
) -> None:
    await conn.execute(
        """
        INSERT INTO event_narratives
            (event_id, campaign_id, narrative, model, prompt_version, context_signature)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id, campaign_id, narrative, model, prompt_version, context_signature,
    )
