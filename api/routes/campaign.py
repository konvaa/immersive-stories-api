"""
api/routes/campaign.py — Endpointy pro správu kampaní.

POST /campaign/create  — vytvoří kampaň s výchozím světem (NPC + lokace).
GET  /campaign/{id}/load — načte snapshot kampaně (ownership check).

Dle GDD: kampaň definuje ruleset a počáteční podmínky světa.

Schémata tabulek:
  campaigns:          id, user_id, template, seed, engine_version, ruleset_version,
                      player_state, world_pressure, tick, time_of_day, created_at, last_played_at
  npcs:               id, campaign_id, template_id, name, description, location_id,
                      trust, fear, suspicion, hp, state_json, created_at
  locations:          id, campaign_id, location_id, name, known_exits, flags,
                      first_visited, created_at
  campaign_snapshots: campaign_id, snapshot_json, tick, updated_at
"""

from __future__ import annotations

import os
import random
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from api.routes.auth import get_current_user_id as _get_user_id
from db.supabase import get_client
from engine.ruleset import ADVENTURER_MAX_HP, CORE_SKILLS, DIFFICULTY_PROFILES
from models.api import CampaignCreate, CampaignLoad

router = APIRouter(prefix="/campaign", tags=["campaign"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "1.0.0") -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# POST /campaign/create
# ---------------------------------------------------------------------------

@router.post("/create", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignCreate,
    user_id: Annotated[str, Depends(_get_user_id)],
) -> dict:
    """
    Vytvoří novou kampaň s výchozím světem.

    1. Vygeneruje seed (náhodný bigint).
    2. Uloží campaign row (template, player_state, world_pressure, time_of_day).
    3. Vytvoří lokaci crossroads_inn (known_exits=['crossroads_exterior']).
    4. Vytvoří NPC: Innkeeper a Sheriff (template_id = Adventurer).
    5. Vytvoří campaign_snapshot (snapshot_json).
    Vrátí campaign_id.
    """
    if body.difficulty not in DIFFICULTY_PROFILES:
        raise HTTPException(
            status_code=400,
            detail=f"Neznámá difficulty '{body.difficulty}' — "
                   f"povolené: {', '.join(DIFFICULTY_PROFILES)}",
        )

    db = get_client()
    seed = random.randint(0, 2**63 - 1)
    start_location = "crossroads_inn"

    # 1. Campaign row — Adventurer škála: HP 20, core skilly na 10 (bonus +0)
    initial_player_state = {
        "player_id": user_id,
        "location_id": start_location,
        "hp": ADVENTURER_MAX_HP,
        "max_hp": ADVENTURER_MAX_HP,
        "gold": 10,
        "flags": [],
        "knowledge": [],
        "skills": {s: 10 for s in CORE_SKILLS},
    }
    camp_res = (
        db.table("campaigns")
        .insert({
            "user_id":         user_id,
            "template":        body.template,
            "seed":            seed,
            "engine_version":  _env("ENGINE_VERSION"),
            "ruleset_version": body.ruleset_override
                               or _env("RULESET_VERSION", "adventurer-0.1.0"),
            "player_state":    initial_player_state,
            "world_pressure":  0,
            "tick":            0,
            "time_of_day":     "morning",
            "difficulty":      body.difficulty,
        })
        .execute()
    )
    if not camp_res.data:
        raise HTTPException(status_code=500, detail="Nepodařilo se vytvořit kampaň")

    campaign_id: str = camp_res.data[0]["id"]

    # 2. Lokace: crossroads_inn
    db.table("locations").insert({
        "campaign_id": campaign_id,
        "location_id": start_location,
        "name":        "Crossroads Inn",
        "known_exits": ["crossroads_exterior"],
        "flags":       [],
    }).execute()

    # 3. NPC — template_id = Adventurer; HP ve stejné škále jako hráč (max 20),
    # aby melee_damage (1d4–1d10+2) dával smysluplné souboje.
    db.table("npcs").insert([
        {
            "campaign_id": campaign_id,
            "template_id": "Adventurer",
            "name":        "Innkeeper",
            "description": "Hospodský za výčepem, přátelský a hovorný.",
            "location_id": start_location,
            "trust":       60,
            "fear":        0,
            "suspicion":   0,
            "hp":          16,
            "state_json":  {"disposition": "friendly", "flags": [], "knowledge": []},
        },
        {
            "campaign_id": campaign_id,
            "template_id": "Adventurer",
            "name":        "Sheriff",
            "description": "Místní strážce zákona, sleduje neznámé.",
            "location_id": start_location,
            "trust":       30,
            "fear":        10,
            "suspicion":   20,
            "hp":          20,
            "state_json":  {"disposition": "neutral", "flags": ["law_enforcer"], "knowledge": []},
        },
    ]).execute()

    # 4. Campaign snapshot
    db.table("campaign_snapshots").insert({
        "campaign_id":   campaign_id,
        "tick":          0,
        "snapshot_json": {
            "player_state":   initial_player_state,
            "world_pressure": 0,
            "time_of_day":    "morning",
            "global_flags":   [],
        },
    }).execute()

    return {"campaign_id": campaign_id}


# ---------------------------------------------------------------------------
# GET /campaign/list
# ---------------------------------------------------------------------------

@router.get("/list")
async def list_campaigns(
    user_id: Annotated[str, Depends(_get_user_id)],
) -> list[dict]:
    """
    Vrátí seznam kampaní přihlášeného uživatele, seřazených od nejnovější.

    Response: [{campaign_id, template, created_at}, ...]
    """
    db = get_client()

    res = (
        db.table("campaigns")
        .select("id, template, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )

    return [
        {
            "campaign_id": row["id"],
            "template":    row["template"],
            "created_at":  row["created_at"],
        }
        for row in (res.data or [])
    ]


# ---------------------------------------------------------------------------
# DELETE /campaign/{campaign_id}
# ---------------------------------------------------------------------------

@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: str,
    user_id: Annotated[str, Depends(_get_user_id)],
) -> None:
    """
    Smaže kampaň a veškerá závislá data (cascade).

    Pořadí mazání (FK závislosti):
      event_visuals + vision_generation_requests (FK na events/campaigns, BEZ cascade)
      → event_narratives → events → campaign_snapshots → npcs → locations → campaigns

    Pozn.: vision_credit_ledger se NEMAŽE — je append-only audit (RLS blokuje DELETE)
    a reference_id není FK, takže smazání requestů nezpůsobí FK violation.
    """
    db = get_client()

    # Ownership check
    camp_res = (
        db.table("campaigns")
        .select("id, user_id")
        .eq("id", campaign_id)
        .single()
        .execute()
    )
    if not camp_res.data:
        raise HTTPException(status_code=404, detail="Kampaň nenalezena")
    if camp_res.data["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Přístup odepřen")

    # Načti event IDs pro cascade event_narratives
    events_res = (
        db.table("events")
        .select("id")
        .eq("campaign_id", campaign_id)
        .execute()
    )
    event_ids = [e["id"] for e in (events_res.data or [])]

    # Cascade delete
    # Nejdřív tabulky s FK na events/campaigns BEZ ON DELETE CASCADE —
    # jinak by smazání events/campaigns spadlo na FK violation (500).
    db.table("event_visuals")              .delete().eq("campaign_id", campaign_id).execute()
    db.table("vision_generation_requests") .delete().eq("campaign_id", campaign_id).execute()
    if event_ids:
        db.table("event_narratives").delete().in_("event_id", event_ids).execute()
    db.table("events")             .delete().eq("campaign_id", campaign_id).execute()
    db.table("campaign_snapshots") .delete().eq("campaign_id", campaign_id).execute()
    db.table("npcs")               .delete().eq("campaign_id", campaign_id).execute()
    db.table("locations")          .delete().eq("campaign_id", campaign_id).execute()
    db.table("campaigns")          .delete().eq("id",          campaign_id).execute()


# ---------------------------------------------------------------------------
# GET /campaign/{campaign_id}/visuals
# ---------------------------------------------------------------------------

@router.get("/{campaign_id}/visuals")
async def list_campaign_visuals(
    campaign_id: str,
    user_id: Annotated[str, Depends(_get_user_id)],
) -> list[dict]:
    """
    Vrátí všechny vygenerované vizuály kampaně (Vision Archive galerie).

    Ownership check → SELECT z event_visuals ORDER BY created_at DESC.
    Vrací list pro Flutter VisionArchiveScreen (čte image_url / original_image_url).
    """
    db = get_client()

    # Ownership check
    camp_res = (
        db.table("campaigns")
        .select("id, user_id")
        .eq("id", campaign_id)
        .single()
        .execute()
    )
    if not camp_res.data:
        raise HTTPException(status_code=404, detail="Kampaň nenalezena")
    if camp_res.data["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Přístup odepřen")

    visuals_res = (
        db.table("event_visuals")
        .select("id, event_id, original_image_url, prompt, provider, test_mode, created_at")
        .eq("campaign_id", campaign_id)
        .order("created_at", desc=True)
        .execute()
    )

    return [
        {
            "visual_id":  row["id"],
            "event_id":   row["event_id"],
            "image_url":  row.get("original_image_url"),
            "prompt":     row.get("prompt"),
            "provider":   row.get("provider"),
            "test_mode":  row.get("test_mode"),
            "created_at": row.get("created_at"),
        }
        for row in (visuals_res.data or [])
    ]


# ---------------------------------------------------------------------------
# GET /campaign/{campaign_id}/load
# ---------------------------------------------------------------------------

@router.get("/{campaign_id}/load", response_model=CampaignLoad)
async def load_campaign(
    campaign_id: str,
    user_id: Annotated[str, Depends(_get_user_id)],
) -> CampaignLoad:
    """
    Načte aktuální snapshot kampaně.

    - Ověří ownership (user_id musí odpovídat campaign.user_id).
    - Vrátí: campaign metadata + snapshot_json + NPC list + locations list.
    """
    db = get_client()

    # Ownership check
    camp_res = (
        db.table("campaigns")
        .select("*")
        .eq("id", campaign_id)
        .single()
        .execute()
    )
    if not camp_res.data:
        raise HTTPException(status_code=404, detail="Kampaň nenalezena")

    campaign = camp_res.data
    if campaign["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Přístup odepřen")

    # Nejnovější snapshot
    snap_res = (
        db.table("campaign_snapshots")
        .select("snapshot_json, tick")
        .eq("campaign_id", campaign_id)
        .order("tick", desc=True)
        .limit(1)
        .execute()
    )
    snapshot_json: dict = snap_res.data[0]["snapshot_json"] if snap_res.data else {}

    # NPC list
    npcs_res = (
        db.table("npcs")
        .select("*")
        .eq("campaign_id", campaign_id)
        .execute()
    )

    # Locations list
    locs_res = (
        db.table("locations")
        .select("*")
        .eq("campaign_id", campaign_id)
        .execute()
    )

    return CampaignLoad(
        campaign_id=campaign_id,
        template=campaign["template"],
        seed=campaign["seed"],
        engine_version=campaign["engine_version"],
        ruleset_version=campaign["ruleset_version"],
        snapshot_json=snapshot_json,
        npcs=npcs_res.data or [],
        locations=locs_res.data or [],
        tick=campaign["tick"],
    )
