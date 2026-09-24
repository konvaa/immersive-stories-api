"""
api/routes/narrative.py — GET /narrative/{event_id}

Flow:
  1. Ověř JWT → user_id
  2. Načti event row → campaign_id; ověř ownership (campaign.user_id == user_id)
  3. SELECT FROM event_narratives WHERE event_id
     a. Existuje  → vrať uložený narativ
     b. Neexistuje → sestav kontext z events + FrozenWorldView, zavolej LLM,
                     ulož do event_narratives, vrať narativ
  4. Resolver se NIKDY nevolá — pouze LLM narátor.

Dle GDD: narativní vrstva je „hlas světa" — překládá herní stav do
přirozeného jazyka pro hráče.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from api.routes.auth import get_current_user_id as _get_user_id
from db.loader import build_frozen_world_view, load_campaign_row
from db.supabase import get_client
from db.writer import insert_event_narrative
from llm.context_builder import build_narrative_context
from llm.narrator import generate_narrative, used_model

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/narrative", tags=["narrative"])


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class NarrativeResponse(BaseModel):
    event_id: str
    campaign_id: str
    narrative: str | None
    from_cache: bool


# ---------------------------------------------------------------------------
# GET /narrative/campaign/{campaign_id}/history
# ---------------------------------------------------------------------------

class HistoryEntry(BaseModel):
    event_id:   str
    raw_intent: str
    narrative:  str | None
    tick:       int


@router.get("/campaign/{campaign_id}/history", response_model=list[HistoryEntry])
async def get_campaign_history(
    campaign_id: str,
    user_id: Annotated[str, Depends(_get_user_id)],
) -> list[HistoryEntry]:
    """
    Vrátí historii eventů kampaně seřazenou podle tick ASC.

    Response: [{event_id, raw_intent, narrative, tick}, ...]
    Pouze eventy s existujícím narrativem (JOIN s event_narratives).
    """
    print(f"[history] campaign_id={campaign_id}")
    print(f"[history] user_id from JWT={user_id}")

    db = get_client()

    # Ownership check
    try:
        campaign = load_campaign_row(db, campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Kampaň nenalezena")

    print(f"[history] campaign row={campaign}")
    print(f"[history] campaign user_id={campaign['user_id']}")

    if campaign["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Přístup odepřen")

    # Načti eventy + jejich narativy
    events_res = (
        db.table("events")
        .select("id, raw_intent, tick")
        .eq("campaign_id", campaign_id)
        .order("tick", desc=False)
        .execute()
    )

    if not events_res.data:
        return []

    event_ids = [e["id"] for e in events_res.data]

    narratives_res = (
        db.table("event_narratives")
        .select("event_id, narrative")
        .in_("event_id", event_ids)
        .execute()
    )

    narrative_map: dict[str, str] = {
        row["event_id"]: row["narrative"]
        for row in (narratives_res.data or [])
    }

    return [
        HistoryEntry(
            event_id   = e["id"],
            raw_intent = e.get("raw_intent") or "",
            narrative  = narrative_map.get(e["id"]),
            tick       = e.get("tick") or 0,
        )
        for e in events_res.data
    ]


# ---------------------------------------------------------------------------
# GET /narrative/{event_id}
# ---------------------------------------------------------------------------

@router.get("/{event_id}", response_model=NarrativeResponse)
async def get_narrative(
    event_id: str,
    user_id: Annotated[str, Depends(_get_user_id)],
) -> NarrativeResponse:
    """
    Vrátí narativ pro daný event.

    Pokud narativ není v cache (event_narratives), vygeneruje ho přes LLM
    a uloží. Resolver se nevolá — event musí již existovat.
    """
    db = get_client()

    # ── KROK 1: Načti event → campaign_id ────────────────────────────────────
    event_res = (
        db.table("events")
        .select("id, campaign_id, action_type, raw_intent, outcome, roll, dc, effects, stakes, mitigated")
        .eq("id", event_id)
        .single()
        .execute()
    )
    if not event_res.data:
        raise HTTPException(status_code=404, detail="Event nenalezen")

    event = event_res.data
    campaign_id: str = event["campaign_id"]

    # ── KROK 2: Ownership check ───────────────────────────────────────────────
    try:
        campaign = load_campaign_row(db, campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Kampaň nenalezena")

    if campaign["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Přístup odepřen")

    # ── KROK 3: Zkus cache ────────────────────────────────────────────────────
    cache_res = (
        db.table("event_narratives")
        .select("narrative")
        .eq("event_id", event_id)
        .limit(1)
        .execute()
    )
    if cache_res.data:
        return NarrativeResponse(
            event_id    = event_id,
            campaign_id = campaign_id,
            narrative   = cache_res.data[0]["narrative"],
            from_cache  = True,
        )

    # ── KROK 4: Vygeneruj narativ přes LLM ───────────────────────────────────
    from models.state import Effect, EffectType  # lazy import

    world = build_frozen_world_view(db, campaign_id, campaign)

    # Deserializuj efekty z JSONB
    raw_effects = event.get("effects") or []
    effects: list[Effect] = []
    for fx in raw_effects:
        try:
            effects.append(Effect(**fx))
        except Exception:
            pass

    context_prompt = build_narrative_context(
        world       = world,
        action_type = event.get("action_type", "UNKNOWN"),
        outcome     = event.get("outcome", "CLEAN_SUCCESS"),
        roll        = event.get("roll", 10),
        dc          = event.get("dc", 10),
        intent      = event.get("raw_intent", ""),
        effects     = effects,
        stakes      = event.get("stakes"),
        mitigated   = bool(event.get("mitigated")),
    )

    narrative = generate_narrative(context_prompt)

    # ── KROK 5: Ulož do event_narratives (pokud LLM uspěl) ───────────────────
    context_signature: str = event.get("context_signature", "")
    if narrative:
        try:
            insert_event_narrative(
                db,
                event_id          = event_id,
                campaign_id       = campaign_id,
                narrative         = narrative,
                context_signature = context_signature,
                model             = used_model(),
            )
        except Exception as exc:
            logger.warning("Uložení narativu selhalo (event_id=%s): %s", event_id, exc)

    return NarrativeResponse(
        event_id    = event_id,
        campaign_id = campaign_id,
        narrative   = narrative,
        from_cache  = False,
    )
