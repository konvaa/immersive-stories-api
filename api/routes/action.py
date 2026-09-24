"""
api/routes/action.py — POST /action endpoint.

Flow dle GDD sekce 20.1 (plně transakční — asyncpg, db/pg.py):
  1.  Auth middleware ověří JWT → user_id
  2.  Ověř campaign.user_id == authenticated user (před lockem, GDD 20.7)
  3.  BEGIN TRANSACTION
  4.  pg_advisory_xact_lock(hashtextextended(campaign_id))
  5.  Idempotency check: SELECT FROM events WHERE request_id (uvnitř locku)
  6.  DB loader: načti npcs, locations, campaigns → FrozenWorldView
  7.  Classifier → action_type; Resolver → ResolutionOutput
  8.  apply_effects in-memory
  9.  INSERT INTO events
  10. UPDATE npcs, campaigns, locations
  11. UPSERT campaign_snapshots
  12. COMMIT (advisory lock auto-released)
  13. Build NarrativeContext → volej Anthropic API (mimo transakci)
  14. INSERT INTO event_narratives (pokud narrative != None)
  15. Return ActionResponse
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from db import pg
from engine.classifier import classify
from engine.effect_applier import apply_effects
from engine.resolver import is_game_over, resolve
from engine.visualizer_rules import is_visualizer_eligible
from llm.context_builder import build_narrative_context
from llm.narrator import generate_narrative, used_model
from models.api import ActionRequest, ActionResponse, UIDelta
from models.state import EffectType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/action", tags=["action"])


# ---------------------------------------------------------------------------
# Auth — plná verifikace JWT podpisu (viz api/routes/auth.py)
# ---------------------------------------------------------------------------

from api.routes.auth import get_current_user_id as _get_user_id  # noqa: E402


# ---------------------------------------------------------------------------
# Visualizer offer cooldown (GDD 23.21 — scarcity)
# ---------------------------------------------------------------------------

async def _visualizer_offer_allowed(pool, campaign_id: str, current_tick: int) -> bool:
    """
    Nabídka vizualizace má cooldown: pokud byl v posledních N ticích úspěšně
    vygenerován vizuál, nenabízej znovu. Scarcity je feature — kdyby se
    nabízelo všechno, nic není významné.
    """
    cooldown = int(os.getenv("VISION_OFFER_COOLDOWN_TICKS", "5"))
    if cooldown <= 0:
        return True
    async with pool.acquire() as conn:
        last_tick = await conn.fetchval(
            """
            SELECT MAX(e.tick)
            FROM vision_generation_requests vgr
            JOIN events e ON e.id = vgr.event_id
            WHERE vgr.campaign_id = $1 AND vgr.status = 'success'
            """,
            campaign_id,
        )
    return last_tick is None or (current_tick - last_tick) >= cooldown


# ---------------------------------------------------------------------------
# UI delta builder
# ---------------------------------------------------------------------------

def _build_ui_deltas(effects: list) -> list[UIDelta]:
    """Mapuje interní Effect objekty na UIDelta pro klienta."""
    deltas = []
    for fx in effects:
        if fx.type == EffectType.DELTA:
            deltas.append(UIDelta(
                type="DELTA", target_id=fx.target_id,
                attribute=fx.attribute, value=fx.value,
            ))
        elif fx.type == EffectType.SET:
            deltas.append(UIDelta(
                type="SET", target_id=fx.target_id,
                attribute=fx.attribute, value=fx.value,
            ))
        elif fx.type in (EffectType.ADD_FLAG, EffectType.REMOVE_FLAG):
            deltas.append(UIDelta(
                type=fx.type.value, target_id=fx.target_id, flag=fx.flag,
            ))
        elif fx.type == EffectType.MOVE_ENTITY:
            deltas.append(UIDelta(
                type="MOVE_ENTITY", target_id=fx.target_id, value=fx.destination,
            ))
        elif fx.type == EffectType.ADD_KNOWLEDGE:
            deltas.append(UIDelta(
                type="ADD_KNOWLEDGE", target_id=fx.target_id, value=fx.knowledge,
            ))
    return deltas


# ---------------------------------------------------------------------------
# POST /action
# ---------------------------------------------------------------------------

@router.post("", response_model=ActionResponse)
async def post_action(
    body: ActionRequest,
    user_id: Annotated[str, Depends(_get_user_id)],
) -> ActionResponse:
    """
    Zpracuje herní akci hráče — transakčně dle GDD 20.1.

    Celý zápis (idempotency → load → resolve → event + effects + snapshot)
    běží v JEDNÉ Postgres transakci pod pg_advisory_xact_lock(campaign_id).
    Idempotentní: opakovaný request se stejným request_id vrátí uložený výsledek.
    Pokud Anthropic API selže, vrátí event_id + outcome, narrative=None.
    """
    pool = await pg.get_pool()

    async with pool.acquire() as conn:
        # ── KROK 2: Ownership check (před lockem — GDD 20.7) ─────────────────
        campaign = await pg.load_campaign_row(conn, body.campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="Kampaň nenalezena")
        if str(campaign["user_id"]) != user_id:
            raise HTTPException(status_code=403, detail="Přístup odepřen")

        # ── KROK 3–11: Jedna transakce ────────────────────────────────────────
        async with conn.transaction():
            # KROK 4: advisory lock (transaction-scoped, auto-release na COMMIT)
            await pg.acquire_campaign_lock(conn, body.campaign_id)

            # KROK 5: Idempotency check — UVNITŘ locku, scoped na kampaň (K2)
            existing = await pg.find_event_by_request_id(
                conn, body.campaign_id, body.request_id,
            )
            if existing is not None:
                logger.info(
                    "Idempotent hit: request_id=%s → event_id=%s",
                    body.request_id, existing["id"],
                )
                narrative = await pg.load_narrative_for_event(conn, existing["id"])
                cached_effects = existing.get("effects") or []
                return ActionResponse(
                    event_id    = str(existing["id"]),
                    narrative   = narrative,
                    outcome     = existing["outcome"],
                    action_type = existing["action_type"],
                    stakes      = existing.get("stakes") or "MEDIUM",
                    mitigated   = bool(existing.get("mitigated")),
                    effects     = cached_effects,
                    ui_delta    = [],
                    # events.tick = tick PŘED commitem; čerstvá odpověď vrací
                    # applied.tick = tick+1 → cache musí být konzistentní (AT-1/12)
                    tick        = existing["tick"] + 1,
                    visualizer_available = is_visualizer_eligible(
                        existing["action_type"], existing["outcome"], cached_effects,
                    ),
                )

            # KROK 6: Načti svět POD lockem → FrozenWorldView
            campaign = await pg.load_campaign_row(conn, body.campaign_id)
            world = await pg.build_frozen_world_view(conn, body.campaign_id, campaign)

            # KROK 6.5: Game over guard (audit K5) — padlý hráč nepokračuje.
            # Záměrně AŽ PO idempotency checku: retry poslední (fatální) akce
            # vrátí cached výsledek, nová akce dostane 409.
            if is_game_over(world.player):
                raise HTTPException(
                    status_code=409,
                    detail="GAME_OVER: hráč padl — kampaň je ukončena. "
                           "Založ novou kampaň.",
                )

            # KROK 7: Klasifikace + resoluce (čisté funkce)
            action_type = classify(body.intent)
            resolution = resolve(world, action_type.value, body.intent, body.request_id)

            # KROK 8: Apply effects in-memory
            applied = apply_effects(world, resolution.effects)

            # KROK 9: INSERT INTO events
            event_row = await pg.insert_event(
                conn,
                campaign_id = body.campaign_id,
                request_id  = body.request_id,
                tick        = world.tick,
                action_type = action_type.value,
                intent      = body.intent,
                outcome     = resolution.outcome,
                effects     = resolution.effects,
                world       = world,
                roll        = resolution.roll,
                dc          = resolution.dc,
                stakes      = resolution.stakes,
                mitigated   = resolution.mitigated,
            )
            event_id = str(event_row["id"])

            # KROK 10+11: Persist effects + snapshot (stejná transakce)
            await pg.apply_applied_state(conn, body.campaign_id, applied)
            await pg.upsert_campaign_snapshot(
                conn, body.campaign_id, applied, resolution.effects
            )
        # ── KROK 12: COMMIT (lock uvolněn) ────────────────────────────────────

    # ── KROK 13: Generuj narativ (LLM, mimo transakci, fallback na None) ─────
    narrative = None
    try:
        context_prompt = build_narrative_context(
            world       = world,
            action_type = action_type.value,
            outcome     = resolution.outcome,
            roll        = resolution.roll,
            dc          = resolution.dc,
            intent      = body.intent,
            effects     = resolution.effects,
            total       = resolution.total,
            stakes      = resolution.stakes,
            mitigated   = resolution.mitigated,
        )
        narrative = generate_narrative(context_prompt)

        # ── KROK 14: INSERT event_narratives ─────────────────────────────────
        if narrative:
            async with pool.acquire() as conn:
                await pg.insert_event_narrative(
                    conn,
                    event_id          = event_id,
                    campaign_id       = body.campaign_id,
                    narrative         = narrative,
                    context_signature = str(event_row.get("context_signature") or ""),
                    model             = used_model(),
                )

    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM narativ selhal (event_id=%s): %s", event_id, exc)
        narrative = None

    # ── KROK 15: Return ActionResponse ────────────────────────────────────────
    vis_available = is_visualizer_eligible(
        action_type.value, resolution.outcome, resolution.effects,
    )
    if vis_available:
        vis_available = await _visualizer_offer_allowed(
            pool, body.campaign_id, applied.tick,
        )

    return ActionResponse(
        event_id    = event_id,
        narrative   = narrative,
        outcome     = resolution.outcome,
        action_type = action_type.value,
        stakes      = resolution.stakes,
        mitigated   = resolution.mitigated,
        effects     = [e.model_dump() for e in resolution.effects],
        ui_delta    = _build_ui_deltas(resolution.effects),
        tick        = applied.tick,
        visualizer_available = vis_available,
    )
