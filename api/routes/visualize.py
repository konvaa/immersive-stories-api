"""
api/routes/visualize.py — POST /visualize/{event_id}

Phase 2A Visualizer — DALL-E 3 image generation.

Flow:
  1. Auth → user_id z JWT
  2. Ownership check: event.campaign_id → campaign.user_id == user_id
  3. Cache check: SELECT FROM event_visuals WHERE event_id
     Hit  → vrátí existující image_url (HTTP 200)
     Miss → pokračuje generováním
  4. Načti event + campaign + npcs + location z DB
  5. Sestaví prompt přes build_image_prompt()
  6. Zavolá DALL-E 3 API → získá image URL
  7. Stáhne obrázek a uloží do Supabase Storage (bucket: event-visuals)
  8. Vytvoří řádek v event_visuals
  9. Vrátí {visual_id, image_url, test_mode: True}
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid as uuid_lib
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from api.routes.auth import get_current_user_id as _get_user_id
from db import credits, pg
from db.supabase import get_client
from llm.image_prompt_builder import build_image_prompt
from models.state import LocationSnapshot, NPCSnapshot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/visualize", tags=["visualize"])

_STORAGE_BUCKET = "event-visuals"


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class VisualizeResponse(BaseModel):
    visual_id:  str
    image_url:  str
    from_cache: bool
    test_mode:  bool
    provider:   str = "dalle3"
    credit_cost: int = 0
    remaining_credits: int | None = None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_npcs_for_campaign(db, campaign_id: str) -> list[NPCSnapshot]:
    res = (
        db.table("npcs")
        .select("id, campaign_id, template_id, name, location_id, "
                "trust, fear, suspicion, hp, state_json")
        .eq("campaign_id", campaign_id)
        .execute()
    )
    snapshots = []
    for row in (res.data or []):
        state = row.get("state_json") or {}
        snapshots.append(NPCSnapshot(
            npc_id      = row["id"],
            campaign_id = row["campaign_id"],
            template_id = row.get("template_id", ""),
            name        = row.get("name", ""),
            location_id = row.get("location_id", ""),
            trust       = row.get("trust", 50),
            fear        = row.get("fear", 0),
            suspicion   = row.get("suspicion", 0),
            hp          = row.get("hp", 100),
            disposition = state.get("disposition", "neutral"),
            flags       = state.get("flags", []),
            knowledge   = state.get("knowledge", []),
        ))
    return snapshots


def _load_location(db, campaign_id: str, location_id: str) -> LocationSnapshot | None:
    res = (
        db.table("locations")
        .select("id, location_id, campaign_id, name, known_exits, flags")
        .eq("campaign_id", campaign_id)
        .eq("location_id", location_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    row = res.data[0]
    return LocationSnapshot(
        row_id      = row["id"],
        location_id = row.get("location_id", row["id"]),
        campaign_id = row["campaign_id"],
        name        = row.get("name", ""),
        known_exits = row.get("known_exits") or [],
        flags       = row.get("flags") or [],
    )


# ---------------------------------------------------------------------------
# POST /visualize/{event_id}
# ---------------------------------------------------------------------------

@router.post("/{event_id}", response_model=VisualizeResponse)
async def visualize_event(
    event_id: str,
    user_id: Annotated[str, Depends(_get_user_id)],
) -> VisualizeResponse:
    """
    Vygeneruje nebo vrátí z cache vizuál pro daný event.
    """
    db = get_client()

    # ── 1. Ownership check ────────────────────────────────────────────────────
    event_res = (
        db.table("events")
        .select("id, campaign_id, raw_intent, action_type, outcome")
        .eq("id", event_id)
        .single()
        .execute()
    )
    if not event_res.data:
        raise HTTPException(status_code=404, detail="Event nenalezen")

    event = event_res.data
    campaign_id: str = event["campaign_id"]

    campaign_res = (
        db.table("campaigns")
        .select("id, user_id, template, player_state")
        .eq("id", campaign_id)
        .single()
        .execute()
    )
    if not campaign_res.data:
        raise HTTPException(status_code=404, detail="Kampaň nenalezena")

    campaign = campaign_res.data
    if campaign["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Přístup odepřen")

    # ── 2. Cache check ────────────────────────────────────────────────────────
    cache_res = (
        db.table("event_visuals")
        .select("id, original_image_url, provider, test_mode")
        .eq("event_id", event_id)
        .limit(1)
        .execute()
    )
    if cache_res.data:
        row = cache_res.data[0]
        return VisualizeResponse(
            visual_id  = row["id"],
            image_url  = row["original_image_url"],
            from_cache = True,
            test_mode  = row.get("test_mode", True),
            provider   = row.get("provider") or "dalle3",
        )

    # ── 2b. Konfigurace providera (před stržením kreditů!) ───────────────────
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY není nastaven.",
        )

    # ── 3. Vision Credits + request idempotency (GDD 23.18) ──────────────────
    test_mode = os.getenv("VISUALIZER_TEST_MODE", "true").lower() == "true"
    credit_cost = 0 if test_mode else int(os.getenv("VISION_SCENE_COST", "1"))
    render_type = "scene"

    pool = await pg.get_pool()
    remaining_credits: int | None = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await credits.lock_user_credits(conn, user_id)
            await credits.ensure_onboarding_grant(conn, user_id)

            req = await conn.fetchrow(
                """
                SELECT id, status, image_url,
                       created_at < now() - interval '10 minutes' AS stale
                FROM vision_generation_requests
                WHERE user_id=$1 AND campaign_id=$2 AND event_id=$3 AND render_type=$4
                """,
                user_id, campaign_id, event_id, render_type,
            )

            # EXISTS + success → vrať existující obrázek, ŽÁDNÉ nové stržení
            if req and req["status"] == "success" and req["image_url"]:
                return VisualizeResponse(
                    visual_id  = str(req["id"]),
                    image_url  = req["image_url"],
                    from_cache = True,
                    test_mode  = test_mode,
                    provider   = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"),
                    credit_cost = 0,
                    remaining_credits = await credits.get_balance(conn, user_id),
                )

            # EXISTS + pending → stále se generuje (pokud není stale po pádu)
            if req and req["status"] == "pending" and not req["stale"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Generování již probíhá — zkus to za chvíli.",
                )

            # NOT EXISTS / failed / stale pending → (znovu) strhni a označ pending
            try:
                if req:
                    gen_request_id = str(req["id"])
                    if req["status"] == "pending":
                        # Stale pending po pádu: už zaplaceno, NEstrhávat znovu
                        remaining_credits = await credits.get_balance(conn, user_id)
                    else:
                        # failed → předchozí stržení bylo refundováno, strhni znovu
                        remaining_credits = await credits.charge(
                            conn, user_id, credit_cost, reference_id=gen_request_id,
                        )
                    await conn.execute(
                        """
                        UPDATE vision_generation_requests
                        SET status='pending', credit_cost=$2, failure_reason=NULL,
                            completed_at=NULL, created_at=now()
                        WHERE id=$1
                        """,
                        gen_request_id, credit_cost,
                    )
                else:
                    gen_request_id = str(await conn.fetchval(
                        """
                        INSERT INTO vision_generation_requests
                            (user_id, campaign_id, event_id, render_type, credit_cost)
                        VALUES ($1,$2,$3,$4,$5)
                        RETURNING id
                        """,
                        user_id, campaign_id, event_id, render_type, credit_cost,
                    ))
                    remaining_credits = await credits.charge(
                        conn, user_id, credit_cost, reference_id=gen_request_id,
                    )
            except credits.InsufficientCreditsError as exc:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=f"Nedostatek Vision Credits (zůstatek {exc.balance}, cena {exc.cost}).",
                )

    async def _fail_generation(reason: str) -> None:
        """Refund + označení requestu jako failed (GDD 23.19)."""
        try:
            async with pool.acquire() as c2:
                async with c2.transaction():
                    await credits.lock_user_credits(c2, user_id)
                    if credit_cost:
                        await credits.refund(
                            c2, user_id, credit_cost, reference_id=gen_request_id,
                        )
                    await c2.execute(
                        """
                        UPDATE vision_generation_requests
                        SET status='failed', failure_reason=$2, completed_at=now()
                        WHERE id=$1
                        """,
                        gen_request_id, reason[:500],
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("[visualize] Refund selhal (request=%s): %s", gen_request_id, exc)

    # ── 3b. Načti herní stav ──────────────────────────────────────────────────
    npcs     = _load_npcs_for_campaign(db, campaign_id)
    player_state = (campaign.get("player_state") or {})
    current_location_id = player_state.get("location_id", "")
    location = _load_location(db, campaign_id, current_location_id)

    # ── 4. Sestav prompt ──────────────────────────────────────────────────────
    prompt = build_image_prompt(
        event    = event,
        campaign = campaign,
        npcs     = npcs,
        location = location,
    )
    logger.info("[visualize] event_id=%s prompt=%r", event_id, prompt[:80])

    # ── 5. Image API ──────────────────────────────────────────────────────────
    image_model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    try:
        import openai  # lazy import
        oa_client = openai.OpenAI(api_key=api_key)
        image_size  = os.getenv("OPENAI_IMAGE_SIZE", "1024x1024")
        gen_response = oa_client.images.generate(
            model  = image_model,
            prompt = prompt,
            size   = image_size,
            n      = 1,
        )
        img_data = gen_response.data[0]
        # gpt-image-1 vrací VŽDY base64 (b64_json); dall-e-3 defaultně URL.
        image_b64: str | None = getattr(img_data, "b64_json", None)
        image_url: str | None = getattr(img_data, "url", None)
        if not image_b64 and not image_url:
            raise RuntimeError("API nevrátilo ani b64_json ani url")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[visualize] Image API (%s) selhal: %s", image_model, exc)
        await _fail_generation(f"image_api: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Image API ({image_model}) selhal: {exc}. Kredity vráceny.",
        ) from exc

    # ── 6. Získej bytes (base64 nebo download) a nahraj do Supabase Storage ───
    storage_path = f"{campaign_id}/{event_id}.png"
    public_url: str | None = None
    try:
        if image_b64:
            image_bytes = base64.b64decode(image_b64)
        else:
            async with httpx.AsyncClient(timeout=60) as http:
                img_response = await http.get(image_url)
                img_response.raise_for_status()
                image_bytes = img_response.content

        db.storage.from_(_STORAGE_BUCKET).upload(
            path         = storage_path,
            file         = image_bytes,
            file_options = {"content-type": "image/png", "upsert": "true"},
        )
        public_url = (
            db.storage.from_(_STORAGE_BUCKET).get_public_url(storage_path)
        )
    except Exception as exc:
        logger.error("[visualize] Storage upload selhal: %s", exc)
        # Fallback — přímá provider URL (dočasná). U base64 odpovědi není.
        public_url = image_url

    if not public_url:
        await _fail_generation("storage_upload_failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Obrázek byl vygenerován, ale uložení do Storage selhalo. Kredity vráceny.",
        )

    # ── 7. Ulož do event_visuals + uzavři generation request ─────────────────
    visual_id = str(uuid_lib.uuid4())
    try:
        db.table("event_visuals").insert({
            "id":                    visual_id,
            "event_id":              event_id,
            "campaign_id":           campaign_id,
            "generation_request_id": gen_request_id,
            "original_image_url":    public_url,
            "prompt":                prompt,
            "provider":              image_model,
            "test_mode":             test_mode,
            "provider_call_count":   1,
        }).execute()
    except Exception as exc:
        logger.warning("[visualize] Zápis do event_visuals selhal: %s", exc)
        # Obrázek byl vygenerován a request bude success — vracíme URL i tak

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vision_generation_requests
                SET status='success', image_url=$2, completed_at=now()
                WHERE id=$1
                """,
                gen_request_id, public_url,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("[visualize] Uzavření requestu selhalo (%s): %s", gen_request_id, exc)

    return VisualizeResponse(
        visual_id  = visual_id,
        image_url  = public_url,
        from_cache = False,
        test_mode  = test_mode,
        provider   = image_model,
        credit_cost = credit_cost,
        remaining_credits = remaining_credits,
    )
