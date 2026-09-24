"""
db/loader.py — Čtení dat z Supabase → Pydantic modely.

Hlavní funkce:
  load_campaign_row(db, campaign_id)  → surový dict z tabulky campaigns
  build_frozen_world_view(db, campaign_id, campaign_row) → FrozenWorldView

Dle GDD: loader je read-only brána k perzistentním datům světa.
"""

from __future__ import annotations

from supabase import Client

from models.state import (
    FrozenWorldView,
    LocationSnapshot,
    NPCSnapshot,
    PlayerSnapshot,
)


def load_campaign_row(db: Client, campaign_id: str) -> dict:
    """Načte jeden řádek z tabulky campaigns. Vyhodí výjimku pokud nenalezen."""
    res = (
        db.table("campaigns")
        .select("*")
        .eq("id", campaign_id)
        .single()
        .execute()
    )
    if not res.data:
        raise ValueError(f"Kampaň {campaign_id} nenalezena")
    return res.data


def _load_npcs(db: Client, campaign_id: str) -> list[NPCSnapshot]:
    # Stabilní pořadí (audit K4) — stejný kontrakt jako db/pg.py
    res = (
        db.table("npcs")
        .select("*")
        .eq("campaign_id", campaign_id)
        .order("created_at")
        .order("id")
        .execute()
    )
    snapshots = []
    for row in (res.data or []):
        state_json: dict = row.get("state_json") or {}
        snapshots.append(NPCSnapshot(
            npc_id      = row["id"],
            campaign_id = campaign_id,
            template_id = row.get("template_id", ""),
            name        = row.get("name", ""),
            location_id = row.get("location_id", ""),
            trust       = row.get("trust", 50),
            fear        = row.get("fear", 0),
            suspicion   = row.get("suspicion", 0),
            hp          = row.get("hp", 100),
            disposition = state_json.get("disposition", "neutral"),
            flags       = state_json.get("flags", []),
            knowledge   = state_json.get("knowledge", []),
        ))
    return snapshots


def _load_locations(db: Client, campaign_id: str) -> list[LocationSnapshot]:
    res = (
        db.table("locations")
        .select("*")
        .eq("campaign_id", campaign_id)
        .order("created_at")
        .order("id")
        .execute()
    )
    snapshots = []
    for row in (res.data or []):
        snapshots.append(LocationSnapshot(
            row_id      = row["id"],
            location_id = row.get("location_id", row["id"]),
            campaign_id = campaign_id,
            name        = row.get("name", ""),
            description = row.get("description", ""),
            known_exits = row.get("known_exits") or [],
            hidden_exits= row.get("hidden_exits") or [],
            flags       = row.get("flags") or [],
        ))
    return snapshots


def build_frozen_world_view(
    db: Client,
    campaign_id: str,
    campaign_row: dict,
) -> FrozenWorldView:
    """
    Sestaví FrozenWorldView z DB dat.

    Args:
        db:           Supabase klient.
        campaign_id:  UUID kampaně.
        campaign_row: Řádek z tabulky campaigns (už načtený — bez dalšího DB volání).
    """
    player_state: dict = campaign_row.get("player_state") or {}
    # Zajistí player_id i pro starší záznamy
    player_state.setdefault("player_id", campaign_row.get("user_id", ""))
    player_state.setdefault("campaign_id", campaign_id)
    player_state.setdefault("location_id", "crossroads_inn")

    player = PlayerSnapshot(**player_state)
    npcs = tuple(_load_npcs(db, campaign_id))
    locations = tuple(_load_locations(db, campaign_id))

    return FrozenWorldView(
        campaign_id     = campaign_id,
        engine_version  = campaign_row.get("engine_version", "1.0.0"),
        ruleset_version = campaign_row.get("ruleset_version", "1.0.0"),
        seed            = campaign_row.get("seed", 0),
        tick            = campaign_row.get("tick", 0),
        world_pressure  = campaign_row.get("world_pressure", 0),
        time_of_day     = campaign_row.get("time_of_day", "morning"),
        difficulty      = campaign_row.get("difficulty") or "normal",
        player          = player,
        npcs            = npcs,
        locations       = locations,
    )
