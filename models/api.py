"""
models/api.py — Request a Response Pydantic modely pro HTTP API.

Dle GDD: oddělení API modelů od state modelů zajišťuje stabilitu kontraktu.

DB schéma (reference):
  campaigns:          id, user_id, template, seed, engine_version, ruleset_version,
                      player_state, world_pressure, tick, time_of_day
  npcs:               id, campaign_id, template_id, name, description, location_id,
                      trust, fear, suspicion, hp, state_json
  locations:          id, campaign_id, location_id, name, known_exits, flags
  campaign_snapshots: campaign_id, snapshot_json, tick, updated_at
  events:             id, campaign_id, request_id, tick, action_type, intent,
                      outcome, effects_json, rng_seed, created_at
  event_narratives:   id, event_id, narrative, model, created_at
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Response model pro GET /health."""
    status: str


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------

class CampaignCreate(BaseModel):
    """Tělo requestu pro vytvoření nové kampaně (POST /campaign/create)."""
    template: str = Field(..., min_length=1, max_length=120,
                          description="Typ / šablona kampaně → campaigns.template")
    ruleset_override: str | None = Field(
        None, description="Přepíše RULESET_VERSION z env; None = env hodnota"
    )
    difficulty: str = Field(
        "normal",
        description="Difficulty tier: story / normal / hard / insane "
                    "(DIFFICULTY_PROFILES v engine/ruleset.py)",
    )


class CampaignLoad(BaseModel):
    """Response pro načtení existující kampaně (GET /campaign/{id}/load)."""
    campaign_id: str
    template: str
    seed: int
    engine_version: str
    ruleset_version: str
    snapshot_json: dict[str, Any]
    npcs: list[dict[str, Any]]
    locations: list[dict[str, Any]]
    tick: int


# ---------------------------------------------------------------------------
# Action  (GDD sekce 20.1)
# ---------------------------------------------------------------------------

class ActionRequest(BaseModel):
    """Tělo requestu pro herní akci (POST /action)."""
    campaign_id: str = Field(..., description="UUID kampaně")
    request_id: str  = Field(..., description="UUID requestu — idempotency key")
    intent: str      = Field(..., min_length=1, max_length=500,
                             description="Volný text popisující záměr hráče")


class UIDelta(BaseModel):
    """Atomická UI změna generovaná po akci."""
    type: str
    target_id: str
    attribute: str | None = None
    value: Any = None
    flag: str | None = None


class ActionResponse(BaseModel):
    """Response po zpracování herní akce."""
    event_id: str
    narrative: str | None        # None pokud LLM selže
    outcome: str                 # OutcomeTier.name — 8 tierů (engine/ruleset.py):
                                 # CATASTROPHIC_FAILURE / HARD_FAILURE / SEVERE_FAILURE /
                                 # FAILURE / SMALL_SUCCESS / CLEAN_SUCCESS /
                                 # CLEAN_SUCCESS_BONUS / EXCEPTIONAL_SUCCESS
                                 # (staré eventy: CRITICAL_SUCCESS/SUCCESS/PARTIAL_SUCCESS)
    action_type: str
    stakes: str = "MEDIUM"       # LOW / MEDIUM / HIGH / EXTREME — narativní strop
    mitigated: bool = False      # natural 20 změkčilo neúspěch (žádný vážný následek)
    effects: list[dict[str, Any]]
    ui_delta: list[UIDelta]
    tick: int
    visualizer_available: bool = False  # Phase 2A hook — True pokud engine vygeneroval vizuál


class ActionError(BaseModel):
    """Chybová odpověď API (4xx / 5xx)."""
    error: str
    detail: str | None = None
    code: str | None = Field(None, description="Strojově čitelný kód, např. CAMPAIGN_NOT_FOUND")
