"""
models/state.py — Interní datové modely herního stavu.

Dle GDD: herní stav je single source of truth pro celý engine.

Mapování na DB schéma:
  campaigns.player_state  → PlayerSnapshot
  npcs row               → NPCSnapshot  (trust/fear/suspicion jako top-level sloupce)
  locations row          → LocationSnapshot
  FrozenWorldView        → sestavený ze všech výše
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Effect types
# ---------------------------------------------------------------------------

class EffectType(str, Enum):
    DELTA         = "DELTA"          # číselná změna atributu (+/-)
    SET           = "SET"            # přímé nastavení hodnoty atributu
    ADD_FLAG      = "ADD_FLAG"       # přidání příznaku entitě
    REMOVE_FLAG   = "REMOVE_FLAG"    # odebrání příznaku entitě
    MOVE_ENTITY   = "MOVE_ENTITY"    # přesun entity do jiné lokace
    DISCOVER_EXIT = "DISCOVER_EXIT"  # odhalení východu v lokaci
    ADD_KNOWLEDGE = "ADD_KNOWLEDGE"  # přidání znalosti hráči / NPC


class Effect(BaseModel):
    """Atomická změna světového stavu vytvořená resolverem."""
    type: EffectType
    target_id: str   = Field(..., description="ID entity ('player', npc_id, 'world', location_id)")
    attribute: str | None = Field(None, description="Název atributu (DELTA/SET)")
    value: Any       = Field(None, description="Hodnota změny nebo cílová hodnota")
    flag: str | None = Field(None, description="Název příznaku (ADD/REMOVE_FLAG)")
    destination: str | None = Field(None, description="Cílová lokace (MOVE_ENTITY)")
    exit_key: str | None = Field(None, description="Klíč východu (DISCOVER_EXIT)")
    knowledge: str | None = Field(None, description="Klíč znalosti (ADD_KNOWLEDGE)")


# ---------------------------------------------------------------------------
# Entity snapshots
# ---------------------------------------------------------------------------

class PlayerSnapshot(BaseModel):
    """Stav hráčské postavy — uložen jako campaigns.player_state (JSONB)."""
    player_id: str
    campaign_id: str
    location_id: str
    hp: int = 100          # legacy default; Adventurer kampaně používají 20 (migrace 004)
    max_hp: int = 100
    gold: int = 0
    flags: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    # Core skilly 0–100 (C1: awareness/resolve/presence/finesse; template může přidat).
    # Prázdný dict = level 0 všude → skill bonus +0 (bezpečné pro legacy kampaně).
    skills: dict[str, int] = Field(default_factory=dict)


class NPCSnapshot(BaseModel):
    """
    Stav NPC — mapuje se z řádku tabulky npcs.
    trust/fear/suspicion jsou top-level sloupce; disposition/flags/knowledge
    jsou uvnitř npcs.state_json.
    """
    npc_id: str                              # npcs.id
    campaign_id: str
    template_id: str                         # npcs.template_id
    name: str
    location_id: str
    trust: int = 50                          # npcs.trust
    fear: int = 0                            # npcs.fear
    suspicion: int = 0                       # npcs.suspicion
    hp: int = 100                            # npcs.hp
    disposition: str = "neutral"             # z npcs.state_json
    flags: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)


class LocationSnapshot(BaseModel):
    """Stav lokace — mapuje se z řádku tabulky locations."""
    row_id: str                              # locations.id (UUID)
    location_id: str                         # locations.location_id (logický klíč)
    campaign_id: str
    name: str
    description: str = ""
    known_exits: list[str] = Field(default_factory=list)
    hidden_exits: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# World view (immutable snapshot předaný enginu)
# ---------------------------------------------------------------------------

class FrozenWorldView(BaseModel):
    """
    Neměnný pohled na stav světa předaný resolverovi.
    Engine pracuje s tímto objektem; změny vrací jako seznam Effect.
    """
    model_config = {"frozen": True}

    campaign_id: str
    engine_version: str
    ruleset_version: str
    seed: int = 0                            # campaigns.seed (pro rng_seed výpočet)
    tick: int = 0
    world_pressure: int = 0
    time_of_day: str = "morning"
    difficulty: str = "normal"               # campaigns.difficulty → DIFFICULTY_PROFILES

    player: PlayerSnapshot
    npcs: tuple[NPCSnapshot, ...] = ()
    locations: tuple[LocationSnapshot, ...] = ()
    global_flags: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Resolution I/O
# ---------------------------------------------------------------------------

class ResolutionInput(BaseModel):
    """Vstup pro resolver: akce hráče + zmrazený stav světa."""
    world: FrozenWorldView
    raw_action: str = Field(..., description="Surový text akce od hráče")
    action_type: str | None = None
    action_params: dict[str, Any] = Field(default_factory=dict)


class ResolutionOutput(BaseModel):
    """Výstup resolveru: seznam efektů + metadata rozhodnutí."""
    effects: list[Effect] = Field(default_factory=list)
    outcome: str = "CLEAN_SUCCESS"    # OutcomeTier.name (engine/ruleset.py, 8 tierů)
    roll: int = 10                    # holý d20 (1–20)
    total: int = 10                   # roll + modifikátory
    dc: int = 10                      # obtížnost z ActionDefinition
    margin: int = 0                   # total - dc
    stakes: str = "MEDIUM"            # Stakes.value — narativní strop důsledku
    mitigated: bool = False           # natural 20 změkčilo neúspěch
    natural: int | None = None        # 1 / 20 / None
    narrative_hint: str = ""          # hint pro LLM narátor
    success: bool = True
    failure_reason: str | None = None
