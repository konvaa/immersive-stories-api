"""
db/writer.py — Zápis a aktualizace dat v Supabase.

Funkce:
  insert_event(...)            → vloží řádek do tabulky events
  apply_applied_state(...)     → UPDATE npcs, campaigns, locations
  upsert_campaign_snapshot(..) → UPSERT campaign_snapshots
  insert_event_narrative(...)  → INSERT do event_narratives

Dle GDD: writer zajišťuje perzistenci světa po každém herním ticku.

Předpokládaná DB schémata:
  events:          id, campaign_id, request_id, tick, action_type, intent,
                   outcome, effects, rng_seed, created_at
  event_narratives: id, event_id, narrative, model, created_at
"""

from __future__ import annotations

import hashlib
import json
import os

from supabase import Client

from engine.effect_applier import AppliedState
from models.state import Effect, FrozenWorldView


_INTENT_MAP = {
    "OBSERVE":     "OBSERVATION",
    "INVESTIGATE": "OBSERVATION",
    "MOVE":        "MOVEMENT",
    "STEALTH":     "MOVEMENT",
    "ATTACK":      "PHYSICAL_ACTION",
    "INTERACT":    "PHYSICAL_ACTION",
    "MANIPULATE":  "PHYSICAL_ACTION",
    "WAIT":        "PHYSICAL_ACTION",
    "INFLUENCE":   "SOCIAL_EXPRESSION",
    "UNKNOWN":     "UNKNOWN",
}


def build_event_payload(
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
    """
    Sestaví event payload (rng_seed + context_signature) — ČISTÁ funkce.
    Sdílená REST i asyncpg cestou, aby oba zápisy byly bitově identické.
    """
    intent_type = _INTENT_MAP.get(action_type, "UNKNOWN")
    engine_version = os.getenv("ENGINE_VERSION", "1.0.0")

    # Stejná formule jako v resolveru (GDD 20.4) — sdílená funkce
    from engine.resolver import build_rng_seed
    rng_seed = build_rng_seed(
        world.seed, request_id, tick,
        world.engine_version, world.ruleset_version,
    )

    context_signature = hashlib.sha256(
        json.dumps({
            "actor":           "player",
            "action_type":     action_type,
            "intent_type":     intent_type,
            "zone_id":         world.player.location_id,
            "engine_version":  engine_version,
            "rng_seed":        rng_seed,
        }, sort_keys=True).encode()
    ).hexdigest()[:64]

    return {
        "campaign_id":       campaign_id,
        "request_id":        request_id,
        "tick":              tick,
        "engine_version":    engine_version,
        "actor":             "player",
        "action_type":       action_type,
        "intent_type":       intent_type,
        "raw_intent":        intent,
        "normalized_intent": intent.lower().strip(),
        "context_signature": context_signature,
        "rng_seed":          rng_seed,
        "roll":              roll,
        "dc":                dc,
        "stakes":            stakes,
        "outcome":           outcome,
        "mitigated":         mitigated,
        "effects":           [e.model_dump() for e in effects],
        "causal_tags":       [],
    }


def insert_event(
    db: Client,
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
    """
    Vloží záznam akce do tabulky events (REST klient).
    Vrátí celý vložený řádek (včetně id generovaného DB).
    """
    payload = build_event_payload(
        campaign_id=campaign_id, request_id=request_id, tick=tick,
        action_type=action_type, intent=intent, outcome=outcome,
        effects=effects, world=world, roll=roll, dc=dc, stakes=stakes,
        mitigated=mitigated,
    )
    res = db.table("events").insert(payload).execute()
    if not res.data:
        raise RuntimeError("INSERT INTO events selhal")
    return res.data[0]


def apply_applied_state(
    db: Client,
    campaign_id: str,
    state: AppliedState,
) -> None:
    """
    Aplikuje AppliedState do DB:
      - UPDATE campaigns (player_state, tick, world_pressure, time_of_day)
      - UPDATE každého NPC (trust, fear, suspicion, hp, state_json)
      - UPDATE locations (flags) kde se změnily
    """
    # 1. Kampaň
    db.table("campaigns").update({
        "player_state":    state.player_state,
        "tick":            state.tick,
        "world_pressure":  state.world_pressure,
        "time_of_day":     state.time_of_day,
    }).eq("id", campaign_id).execute()

    # 2. NPC deltas
    for delta in state.npc_deltas:
        db.table("npcs").update({
            "trust":      delta.trust,
            "fear":       delta.fear,
            "suspicion":  delta.suspicion,
            "hp":         delta.hp,
            "state_json": delta.state_json,
        }).eq("id", delta.npc_id).execute()

    # 3. Location flags
    for loc in state.location_flag_updates:
        db.table("locations").update({
            "flags": loc["flags"],
        }).eq("id", loc["row_id"]).execute()


def upsert_campaign_snapshot(
    db: Client,
    campaign_id: str,
    state: AppliedState,
    effects: list[Effect],
) -> None:
    """UPSERT do campaign_snapshots — jeden řádek na kampaň."""
    snapshot_json = {
        "player_state":   state.player_state,
        "world_pressure": state.world_pressure,
        "time_of_day":    state.time_of_day,
        "global_flags":   [],
        "last_effects":   [e.model_dump() for e in effects],
    }
    db.table("campaign_snapshots").upsert(
        {
            "campaign_id":   campaign_id,
            "tick":          state.tick,
            "snapshot_json": snapshot_json,
        },
        on_conflict="campaign_id",
    ).execute()


def insert_event_narrative(
    db: Client,
    event_id: str,
    campaign_id: str,
    narrative: str,
    context_signature: str,
    model: str = "claude-haiku-4-5-20251001",
    prompt_version: str = "1.0.0",
) -> None:
    """INSERT narativu do event_narratives."""
    db.table("event_narratives").insert({
        "event_id":          event_id,
        "campaign_id":       campaign_id,
        "narrative":         narrative,
        "model":             model,
        "prompt_version":    prompt_version,
        "context_signature": context_signature,
    }).execute()
