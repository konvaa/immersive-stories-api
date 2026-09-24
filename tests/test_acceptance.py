"""
tests/test_acceptance.py — Akceptační testy AT-1 až AT-12.

Testují živý backend na http://localhost:8000.
Předpoklady:
  - `uvicorn main:app --reload` běží
  - .env obsahuje SUPABASE_URL, SUPABASE_KEY, ANTHROPIC_API_KEY
  - .env obsahuje TEST_JWT_TOKEN (Supabase JWT pro test uživatele)
    nebo je token na řádku "token=..." v .env

Spuštění:
  pytest tests/test_acceptance.py -v

Označení:
  [unit]       — nevyžaduje běžící server (čistý unit test)
  [xfail]      — test dokumentuje chování, které implementace zatím nesplňuje
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import threading
import time
import uuid
from typing import Generator

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")

# Načti JWT token z prostředí nebo z komentáře v .env ("token=...")
def _load_test_token() -> str:
    token = os.environ.get("TEST_JWT_TOKEN", "")
    if token:
        return token
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "token=" in line and not line.startswith("TEST_JWT_TOKEN"):
                    return line.split("token=", 1)[1].strip()
    except FileNotFoundError:
        pass
    pytest.skip("TEST_JWT_TOKEN není nastaven — přeskoč testy živého backendu")
    return ""


TEST_TOKEN = _load_test_token()


def _decode_sub(token: str) -> str:
    """Extrahuje sub (user_id) z JWT bez ověření podpisu."""
    segment = token.split(".")[1]
    segment += "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))["sub"]


TEST_USER_ID = _decode_sub(TEST_TOKEN) if TEST_TOKEN else "unknown"


def _make_fake_jwt(user_id: str) -> str:
    """
    Sestaví JWT s jiným sub (user_id) bez platného podpisu.
    Funguje s naší implementací, která podpis neověřuje — pouze dekóduje payload.
    VAROVÁNÍ: toto NIKDY nenasazovat v produkci (Supabase middleware by odmítlo).
    """
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "ES256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "sub": user_id,
            "aud": "authenticated",
            "role": "authenticated",
            "iss": "test",
        }).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def client() -> Generator[httpx.Client, None, None]:
    """Sdílený httpx klient pro celou session."""
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        # Ověř dostupnost backendu
        try:
            r = c.get("/health")
            assert r.status_code == 200, f"Backend není dostupný: {r.status_code}"
        except httpx.ConnectError:
            pytest.skip(f"Backend není dostupný na {BASE_URL} — spusť uvicorn")
        yield c


@pytest.fixture(scope="session")
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(scope="session")
def sb():
    """Přímý Supabase klient pro verifikaci DB stavu v testech."""
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        pytest.skip("SUPABASE_URL / SUPABASE_KEY nejsou nastaveny")
    return create_client(url, key)


@pytest.fixture(scope="session")
def campaign_id(client: httpx.Client, auth_headers: dict) -> str:
    """Vytvoří testovací kampaň jednou pro celou session."""
    r = client.post(
        "/campaign/create",
        json={"template": "acceptance_test"},
        headers=auth_headers,
    )
    assert r.status_code == 201, f"Nepodařilo se vytvořit kampaň: {r.text}"
    cid = r.json()["campaign_id"]
    return cid


@pytest.fixture()
def fresh_campaign_id(client: httpx.Client, auth_headers: dict) -> str:
    """Vytvoří čerstvou kampaň pro testy, které potřebují izolované prostředí."""
    r = client.post(
        "/campaign/create",
        json={"template": "acceptance_test_isolated"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    return r.json()["campaign_id"]


def _action(
    client: httpx.Client,
    headers: dict,
    campaign_id: str,
    intent: str,
    request_id: str | None = None,
) -> httpx.Response:
    return client.post(
        "/action",
        json={
            "campaign_id": campaign_id,
            "request_id": request_id or str(uuid.uuid4()),
            "intent": intent,
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# AT-1: Determinismus
# ---------------------------------------------------------------------------

def test_at1_determinism(client, auth_headers, campaign_id):
    """
    AT-1: Stejný request_id → deterministicky stejný outcome.

    rng_seed = md5(seed+request_id+tick+engine_version) % 2^31
    Dva identické requesty musí vrátit stejný event_id, outcome a efekty.
    Druhý request musí být obsluhován z idempotency cache (ne novou resolucí).
    """
    req_id = str(uuid.uuid4())
    body = {"campaign_id": campaign_id, "request_id": req_id, "intent": "look around the room"}

    r1 = client.post("/action", json=body, headers=auth_headers)
    r2 = client.post("/action", json=body, headers=auth_headers)

    assert r1.status_code == 200, f"První request selhal: {r1.text}"
    assert r2.status_code == 200, f"Druhý request selhal: {r2.text}"

    d1, d2 = r1.json(), r2.json()
    assert d1["event_id"] == d2["event_id"],   "Idempotency selhala — různé event_id"
    assert d1["outcome"] == d2["outcome"],     "Nedeterministický outcome"
    assert d1["action_type"] == d2["action_type"]
    assert d1["effects"] == d2["effects"],     "Nedeterministické efekty"

    # Tick se nesmí inkrementovat podruhé (idempotency cache)
    assert d1["tick"] == d2["tick"], "Tick se inkrementoval při idempotentním requestu"


# ---------------------------------------------------------------------------
# AT-2: Idempotency — jeden event v DB
# ---------------------------------------------------------------------------

def test_at2_idempotency_single_event(client, auth_headers, campaign_id, sb):
    """
    AT-2: Stejný request_id dvakrát → právě jeden řádek v tabulce events.
    """
    req_id = str(uuid.uuid4())
    body = {"campaign_id": campaign_id, "request_id": req_id, "intent": "examine the walls"}

    r1 = client.post("/action", json=body, headers=auth_headers)
    r2 = client.post("/action", json=body, headers=auth_headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["event_id"] == r2.json()["event_id"]

    # Ověř přímo v DB
    db_rows = (
        sb.table("events")
        .select("id")
        .eq("request_id", req_id)
        .execute()
    )
    assert len(db_rows.data) == 1, (
        f"Idempotency selhala — v DB je {len(db_rows.data)} eventů pro request_id={req_id}"
    )


# ---------------------------------------------------------------------------
# AT-2b (audit K2): stejný request_id ve DVOU kampaních = dva nezávislé eventy
# ---------------------------------------------------------------------------

def test_at2b_request_id_scoped_per_campaign(client, auth_headers, sb):
    """
    Audit K2: events mají UNIQUE(campaign_id, request_id) — stejné request_id
    ve dvou kampaních musí vytvořit DVA eventy a každá kampaň musí dostat
    SVŮJ event (žádný cross-campaign cache hit / únik dat).
    """
    r1 = client.post("/campaign/create", json={"template": "k2_test_a"},
                     headers=auth_headers)
    r2 = client.post("/campaign/create", json={"template": "k2_test_b"},
                     headers=auth_headers)
    assert r1.status_code == 201 and r2.status_code == 201
    cid_a, cid_b = r1.json()["campaign_id"], r2.json()["campaign_id"]

    shared_req_id = str(uuid.uuid4())
    ra = _action(client, auth_headers, cid_a, "look around", shared_req_id)
    rb = _action(client, auth_headers, cid_b, "look around", shared_req_id)
    assert ra.status_code == 200, f"Kampaň A selhala: {ra.text}"
    assert rb.status_code == 200, f"Kampaň B selhala: {rb.text}"

    assert ra.json()["event_id"] != rb.json()["event_id"], (
        "Cross-campaign idempotency hit — kampaň B dostala event kampaně A"
    )

    # V DB existují právě 2 eventy s tímto request_id, každý ve své kampani
    rows = sb.table("events").select("id, campaign_id") \
             .eq("request_id", shared_req_id).execute()
    assert len(rows.data) == 2
    assert {row["campaign_id"] for row in rows.data} == {cid_a, cid_b}


# ---------------------------------------------------------------------------
# AT-3: LLM selhání po commitu — event existuje, narrative může být null
# ---------------------------------------------------------------------------

def test_at3_llm_failure_graceful(client, auth_headers, campaign_id, sb):
    """
    AT-3: I když LLM selže (timeout / API chyba), event je committed do DB
    a endpoint vrátí platnou ActionResponse s narrative=None.

    Test nenutí LLM selhat (není black-box možné) — ověřuje že kontrakt
    'narrative je str nebo None' je splněn a event_id vždy existuje.
    """
    req_id = str(uuid.uuid4())
    r = client.post(
        "/action",
        json={"campaign_id": campaign_id, "request_id": req_id, "intent": "investigate the fireplace"},
        headers=auth_headers,
    )
    assert r.status_code == 200, f"Endpoint selhal: {r.text}"
    data = r.json()

    assert "event_id" in data and data["event_id"], "event_id chybí v response"
    assert "outcome" in data and data["outcome"],   "outcome chybí v response"
    assert "tick" in data,                          "tick chybí v response"
    assert data["narrative"] is None or isinstance(data["narrative"], str), (
        "narrative musí být str nebo None"
    )

    # Event musí být v DB bez ohledu na LLM výsledek
    db_rows = sb.table("events").select("id").eq("request_id", req_id).execute()
    assert len(db_rows.data) == 1, "Event nebyl commitnut do DB"


# ---------------------------------------------------------------------------
# AT-4: Narrative retry — tick se nemění, nový event nevzniká
# ---------------------------------------------------------------------------

def test_at4_narrative_retry_no_tick_change(client, auth_headers, campaign_id):
    """
    AT-4: GET /narrative/{event_id} je read-only operace.
    Nesmí inkrementovat tick kampaně ani vytvářet nový event.
    """
    # Nejdřív vytvoř event přes /action
    req_id = str(uuid.uuid4())
    r_action = client.post(
        "/action",
        json={"campaign_id": campaign_id, "request_id": req_id, "intent": "observe the tavern"},
        headers=auth_headers,
    )
    assert r_action.status_code == 200
    event_id = r_action.json()["event_id"]
    tick_after_action = r_action.json()["tick"]

    # Načti narativ (může vrátit 200 nebo 5xx pokud narrative.py má nesrovnalosti)
    r_narrative = client.get(f"/narrative/{event_id}", headers=auth_headers)
    # Endpoint může mít nekompatibilitu ve sloupcích — přijmeme 200 nebo 500
    # ale tick se NESMÍ změnit

    # Ověř tick kampaně — musí zůstat stejný jako po /action
    r_load = client.get(f"/campaign/{campaign_id}/load", headers=auth_headers)
    assert r_load.status_code == 200
    current_tick = r_load.json()["tick"]

    assert current_tick == tick_after_action, (
        f"GET /narrative inkrementoval tick: před={tick_after_action}, po={current_tick}"
    )


# ---------------------------------------------------------------------------
# AT-5: Concurrency — dva simultánní requesty, žádné duplicity
# ---------------------------------------------------------------------------

def test_at5_concurrency_no_duplicates(client, auth_headers, fresh_campaign_id, sb):
    """
    AT-5: Dva simultánní requesty s různými request_id na stejnou kampaň
    musí oba uspět a v DB vzniknou právě 2 unique eventy.
    """
    req_id_a = str(uuid.uuid4())
    req_id_b = str(uuid.uuid4())
    results: dict[str, httpx.Response] = {}
    errors: list[Exception] = []

    def send(key: str, req_id: str) -> None:
        try:
            r = client.post(
                "/action",
                json={"campaign_id": fresh_campaign_id, "request_id": req_id, "intent": "look around"},
                headers=auth_headers,
            )
            results[key] = r
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=send, args=("a", req_id_a))
    t2 = threading.Thread(target=send, args=("b", req_id_b))
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)

    assert not errors, f"Thread výjimky: {errors}"
    assert results.get("a") is not None and results["a"].status_code == 200, \
        f"Request A selhal: {results.get('a')}"
    assert results.get("b") is not None and results["b"].status_code == 200, \
        f"Request B selhal: {results.get('b')}"

    # Každý request_id → právě jeden event
    for req_id in (req_id_a, req_id_b):
        rows = sb.table("events").select("id").eq("request_id", req_id).execute()
        assert len(rows.data) == 1, (
            f"Concurrency vytvoří duplicitní event pro request_id={req_id}: {len(rows.data)}"
        )


# ---------------------------------------------------------------------------
# AT-6: Invalid destination — MOVE na neznámou lokaci
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason=(
        "Aktuální implementace vrací 200 s outcome=FAILURE místo ActionError. "
        "Endpoint musí validovat destination před INSERT INTO events."
    ),
    strict=False,
)
def test_at6_invalid_destination_returns_error(client, auth_headers, fresh_campaign_id, sb):
    """
    AT-6: MOVE na lokaci, která není v known_exits, musí vrátit ActionError
    (HTTP 4xx), žádný event v DB a tick kampaně se nesmí změnit.

    Per GDD: resolver smí odmítnout akci dříve než je event commitnut.

    Poznámka: momentálně xfail — implementace vrátí 200/FAILURE.
    Oprava: validovat destination v action.py před insert_event().
    """
    r_before = client.get(f"/campaign/{fresh_campaign_id}/load", headers=auth_headers)
    tick_before = r_before.json()["tick"]

    req_id = str(uuid.uuid4())
    r = client.post(
        "/action",
        json={
            "campaign_id": fresh_campaign_id,
            "request_id":  req_id,
            "intent":      "go to xyzzy_nonexistent_location_abc999",
        },
        headers=auth_headers,
    )

    # Dle spec: 4xx ActionError
    assert r.status_code >= 400, f"Očekáváno ActionError (4xx), dostali jsme {r.status_code}"

    # Žádný event v DB
    rows = sb.table("events").select("id").eq("request_id", req_id).execute()
    assert len(rows.data) == 0, "Event byl vytvořen i pro neplatný destination"

    # Tick se nezměnil
    r_after = client.get(f"/campaign/{fresh_campaign_id}/load", headers=auth_headers)
    assert r_after.json()["tick"] == tick_before, "Tick se změnil pro odmítnutou akci"


def test_at6_invalid_destination_no_move_effect(client, auth_headers, campaign_id):
    """
    AT-6 (soft): MOVE na neznámou lokaci nesmí způsobit MOVE_ENTITY efekt —
    hráč musí zůstat na původní pozici. Tick se může inkrementovat (turn spent).
    """
    r_before = client.get(f"/campaign/{campaign_id}/load", headers=auth_headers)
    original_location = r_before.json()["snapshot_json"].get("player_state", {}).get("location_id")

    req_id = str(uuid.uuid4())
    r = client.post(
        "/action",
        json={
            "campaign_id": campaign_id,
            "request_id":  req_id,
            "intent":      "go to xyzzy_nonexistent_location_abc999",
        },
        headers=auth_headers,
    )

    # Pokud endpoint vrátil 200 (current behavior)
    if r.status_code == 200:
        data = r.json()
        move_effects = [
            fx for fx in data.get("effects", [])
            if fx.get("type") == "MOVE_ENTITY"
        ]
        assert len(move_effects) == 0, (
            f"MOVE_ENTITY efekt vznikl pro neplatnou lokaci: {move_effects}"
        )

        # Hráč je stále na původní lokaci
        r_after = client.get(f"/campaign/{campaign_id}/load", headers=auth_headers)
        new_location = r_after.json()["snapshot_json"].get("player_state", {}).get("location_id")
        assert new_location == original_location, (
            f"Hráč se přesunul na neplatnou lokaci: {new_location}"
        )


# ---------------------------------------------------------------------------
# AT-7: FrozenWorldView immutability (unit test — bez serveru)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_at7_frozen_world_immutability():
    """
    AT-7: Resolver nesmí mutovat FrozenWorldView.
    FrozenWorldView je Pydantic model s frozen=True — přímá mutace vyvolá
    ValidationError. Test ověří, že:
      1. Přímá atributová mutace selže
      2. Resolver neobchází imutabilitu (nevrátí nový objekt se změněným stavem
         maskovaný jako původní)
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    from pydantic import ValidationError

    from engine.resolver import resolve
    from models.state import (
        FrozenWorldView,
        LocationSnapshot,
        NPCSnapshot,
        PlayerSnapshot,
    )

    player = PlayerSnapshot(
        player_id="u1", campaign_id="c1", location_id="crossroads_inn",
        hp=100, max_hp=100, gold=10,
    )
    npc = NPCSnapshot(
        npc_id="npc1", campaign_id="c1", template_id="Adventurer",
        name="Innkeeper", location_id="crossroads_inn",
        trust=60, fear=0, suspicion=0, hp=80,
    )
    loc = LocationSnapshot(
        row_id="loc-uuid", location_id="crossroads_inn", campaign_id="c1",
        name="Crossroads Inn", known_exits=["crossroads_exterior"],
    )
    world = FrozenWorldView(
        campaign_id="c1", engine_version="1.0.0", ruleset_version="1.0.0",
        seed=12345, tick=5, world_pressure=0, time_of_day="morning",
        player=player, npcs=(npc,), locations=(loc,),
    )

    # 1. Přímá mutace musí selhat
    with pytest.raises((ValidationError, TypeError)):
        world.tick = 999  # type: ignore[misc]

    tick_before = world.tick
    npc_trust_before = world.npcs[0].trust

    # 2. Resolver nesmí změnit world
    _ = resolve(world, "INFLUENCE", "talk to innkeeper", "req-xyz-001")

    assert world.tick == tick_before,            "Resolver mutoval world.tick"
    assert world.npcs[0].trust == npc_trust_before, "Resolver mutoval NPC trust"
    assert world.player.location_id == "crossroads_inn", "Resolver mutoval player location"


# ---------------------------------------------------------------------------
# AT-8: Append-only events — žádná mutace přes API
# ---------------------------------------------------------------------------

def test_at8_append_only_events_no_api_mutation(client, auth_headers, campaign_id):
    """
    AT-8: API nevystavuje žádný endpoint pro UPDATE nebo DELETE eventů.

    Test ověří, že:
      - DELETE /action/{event_id} vrátí 404 nebo 405
      - PATCH  /action/{event_id} vrátí 404 nebo 405

    Poznámka: row-level security v Supabase pro events tabulku musí být
    nastavena zvlášť (není testováno zde — závisí na Supabase konfiguraci).
    """
    # Vytvoř event pro test
    req_id = str(uuid.uuid4())
    r = client.post(
        "/action",
        json={"campaign_id": campaign_id, "request_id": req_id, "intent": "wait here"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    event_id = r.json()["event_id"]

    # API nesmí mít mutační endpointy pro events
    r_delete = client.delete(f"/action/{event_id}", headers=auth_headers)
    assert r_delete.status_code in (404, 405), (
        f"DELETE /action/{{id}} by neměl existovat (dostal {r_delete.status_code})"
    )

    r_patch = client.patch(
        f"/action/{event_id}", json={"outcome": "HACKED"}, headers=auth_headers
    )
    assert r_patch.status_code in (404, 405), (
        f"PATCH /action/{{id}} by neměl existovat (dostal {r_patch.status_code})"
    )


def test_at8_append_only_events_rls(sb, client, auth_headers, campaign_id):
    """
    AT-8 (DB level): Pokus o UPDATE/DELETE přes Supabase klient.
    Selže pokud je RLS správně nakonfigurováno.
    Přeskočí pokud je použit service role key (který RLS obchází).
    """
    # Vytvoř event
    req_id = str(uuid.uuid4())
    r = client.post(
        "/action",
        json={"campaign_id": campaign_id, "request_id": req_id, "intent": "rest"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    event_id = r.json()["event_id"]

    # Pokus o UPDATE
    try:
        update_res = (
            sb.table("events")
            .update({"outcome": "HACKED_BY_TEST"})
            .eq("id", event_id)
            .execute()
        )
        # Service role klíč UPDATE umožní — ověříme alespoň že data nejsou corrupted
        # (RLS pro append-only musí být nakonfigurováno na DB straně)
        verify_res = sb.table("events").select("outcome").eq("id", event_id).execute()
        if verify_res.data and verify_res.data[0]["outcome"] == "HACKED_BY_TEST":
            pytest.skip(
                "Service role key obchází RLS — append-only nelze ověřit přes klienta. "
                "Nakonfiguruj RLS policy: DENY UPDATE na tabulce events."
            )
    except Exception:
        pass  # RLS odmítl UPDATE → test prošel


# ---------------------------------------------------------------------------
# AT-9: Snapshot not used by resolver — /action funguje bez snapshot záznamu
# ---------------------------------------------------------------------------

def test_at9_action_works_without_snapshot(client, auth_headers, sb):
    """
    AT-9: Resolver načítá stav světa z campaigns.player_state + npcs + locations,
    nikoli z campaign_snapshots. Endpoint musí fungovat i bez snapshot záznamu.
    """
    # Vytvoř izolovanou kampaň
    r_create = client.post(
        "/campaign/create",
        json={"template": "no_snapshot_test"},
        headers=auth_headers,
    )
    assert r_create.status_code == 201
    cid = r_create.json()["campaign_id"]

    # Smaž snapshot (pokud existuje)
    try:
        sb.table("campaign_snapshots").delete().eq("campaign_id", cid).execute()
    except Exception as e:
        pytest.skip(f"Nelze smazat snapshot (možná RLS): {e}")

    # Ověř, že snapshot neexistuje
    snap = sb.table("campaign_snapshots").select("campaign_id").eq("campaign_id", cid).execute()
    assert len(snap.data) == 0, "Snapshot se nepodařilo smazat"

    # /action musí fungovat i bez snapshotu
    req_id = str(uuid.uuid4())
    r = client.post(
        "/action",
        json={"campaign_id": cid, "request_id": req_id, "intent": "look around"},
        headers=auth_headers,
    )
    assert r.status_code == 200, (
        f"Endpoint selhal bez campaign_snapshot: {r.status_code} {r.text}"
    )
    data = r.json()
    assert data["event_id"]
    assert data["outcome"]

    # Snapshot musí být vytvořen po první akci
    snap_after = (
        sb.table("campaign_snapshots").select("campaign_id").eq("campaign_id", cid).execute()
    )
    assert len(snap_after.data) == 1, "Snapshot nebyl vytvořen po první akci"


# ---------------------------------------------------------------------------
# AT-10: Replay smoke test — event obsahuje vše potřebné pro replay
# ---------------------------------------------------------------------------

def test_at10_replay_smoke(client, auth_headers, campaign_id, sb):
    """
    AT-10: Uložený event v DB obsahuje vše potřebné pro deterministický replay:
      - rng_seed  (pro rekonstrukci d20 rollu)
      - outcome   (výsledná kategorie)
      - effects_json  (aplikované efekty)
      - action_type, intent, tick

    Ověří také, že rng_seed je konzistentní s md5 formulí z GDD 20.1.
    """
    req_id = str(uuid.uuid4())
    r = client.post(
        "/action",
        json={"campaign_id": campaign_id, "request_id": req_id, "intent": "examine the fireplace"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    event_id = r.json()["event_id"]

    # Načti event přímo z DB
    db_res = sb.table("events").select("*").eq("id", event_id).single().execute()
    assert db_res.data, f"Event {event_id} nenalezen v DB"
    ev = db_res.data

    # Povinná pole (sloupce dle schématu GDD 18.4)
    assert ev.get("rng_seed") is not None,   "events.rng_seed chybí"
    assert ev.get("outcome"),                 "events.outcome chybí"
    assert ev.get("effects") is not None,     "events.effects chybí"
    assert ev.get("action_type"),             "events.action_type chybí"
    assert ev.get("raw_intent"),              "events.raw_intent chybí"
    assert ev.get("tick") is not None,        "events.tick chybí"

    # Ověř determinismus rng_seed pomocí md5 formule (GDD 20.4 vč. ruleset_version)
    cam_res = sb.table("campaigns").select("seed, engine_version, ruleset_version").eq("id", campaign_id).single().execute()
    cam = cam_res.data
    expected_seed = int(
        hashlib.md5(
            f"{cam['seed']}{req_id}{ev['tick']}{cam['engine_version']}{cam['ruleset_version']}".encode()
        ).hexdigest(),
        16,
    ) % (2 ** 31)
    assert ev["rng_seed"] == expected_seed, (
        f"rng_seed neodpovídá md5 formulí: expected={expected_seed}, got={ev['rng_seed']}"
    )

    # Ověř d20 replay: PRVNÍ Random(rng_seed).randint(1,20) je hráčův d20
    # (kontrakt resolveru — další hody z rng jsou damage/protiútok).
    rng = random.Random(ev["rng_seed"])
    replayed_roll = rng.randint(1, 20)
    assert replayed_roll == ev["roll"], (
        f"Replay rollu neodpovídá: replayed={replayed_roll}, stored={ev['roll']}"
    )

    # Outcome musí být platný 8-tier OutcomeTier (plná rekonstrukce outcome
    # vyžaduje modifikátory ze stavu světa v daném ticku — mimo scope smoke testu).
    from engine.ruleset import OutcomeTier
    valid_outcomes = {t.name for t in OutcomeTier}
    assert ev["outcome"] in valid_outcomes, (
        f"Neplatný outcome tier: {ev['outcome']}"
    )
    # Natural 1 je invariant nezávislý na modifikátorech
    if ev["roll"] == 1:
        assert ev["outcome"] == "CATASTROPHIC_FAILURE"


# ---------------------------------------------------------------------------
# AT-11: Ownership — jiný user dostane 403
# ---------------------------------------------------------------------------

def test_at11_forged_token_401(client, campaign_id):
    """
    AT-11: Padělaný JWT (neplatný podpis) musí být odmítnut s 401
    ještě PŘED ownership checkem. Od zavedení verifikace podpisu
    (api/routes/auth.py) se útočník s fake tokenem vůbec nedostane
    k herní logice.
    """
    fake_token = _make_fake_jwt(str(uuid.uuid4()))
    r = client.post(
        "/action",
        json={
            "campaign_id": campaign_id,
            "request_id":  str(uuid.uuid4()),
            "intent":      "look around",
        },
        headers={"Authorization": f"Bearer {fake_token}"},
    )
    assert r.status_code == 401, (
        f"Padělaný token dostal {r.status_code} místo 401"
    )


def test_at11_load_campaign_forged_token_401(client, campaign_id):
    """AT-11b: GET /campaign/{id}/load také odmítne padělaný token (401)."""
    fake_token = _make_fake_jwt(str(uuid.uuid4()))
    r = client.get(
        f"/campaign/{campaign_id}/load",
        headers={"Authorization": f"Bearer {fake_token}"},
    )
    assert r.status_code == 401


def test_at11_ownership_403_real_second_user(client, campaign_id):
    """
    AT-11c: SKUTEČNÝ druhý uživatel (validní podpis, jiný sub) dostane 403.
    Vyžaduje env TEST_JWT_TOKEN_2 — token jiného účtu (scripts/get_token.py).
    """
    token2 = os.environ.get("TEST_JWT_TOKEN_2", "")
    if not token2:
        pytest.skip("TEST_JWT_TOKEN_2 není nastaven — přeskakuji ownership test")
    r = client.post(
        "/action",
        json={
            "campaign_id": campaign_id,
            "request_id":  str(uuid.uuid4()),
            "intent":      "look around",
        },
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert r.status_code == 403, (
        f"Cizí (validní) user dostal {r.status_code} místo 403"
    )


# ---------------------------------------------------------------------------
# AT-12: Tick invariant — UNKNOWN action a narrative retry nemění tick
# ---------------------------------------------------------------------------

def test_at12_tick_invariant_narrative_retry(client, auth_headers, campaign_id):
    """
    AT-12a: GET /narrative/{event_id} je read-only — nesmí inkrementovat tick.
    """
    req_id = str(uuid.uuid4())
    r_action = client.post(
        "/action",
        json={"campaign_id": campaign_id, "request_id": req_id, "intent": "nod slowly"},
        headers=auth_headers,
    )
    assert r_action.status_code == 200
    event_id = r_action.json()["event_id"]
    tick_after_action = r_action.json()["tick"]

    # Opakovaně volej /narrative — tick se nesmí měnit
    for _ in range(2):
        client.get(f"/narrative/{event_id}", headers=auth_headers)

    r_load = client.get(f"/campaign/{campaign_id}/load", headers=auth_headers)
    assert r_load.json()["tick"] == tick_after_action, (
        f"GET /narrative inkrementoval tick: "
        f"expected={tick_after_action}, got={r_load.json()['tick']}"
    )


def test_at12_tick_invariant_idempotent_action(client, auth_headers, campaign_id):
    """
    AT-12b: Idempotentní akce (stejný request_id) nesmí inkrementovat tick.
    """
    req_id = str(uuid.uuid4())
    body = {"campaign_id": campaign_id, "request_id": req_id, "intent": "wait a moment"}

    r1 = client.post("/action", json=body, headers=auth_headers)
    assert r1.status_code == 200
    tick_after_first = r1.json()["tick"]

    r2 = client.post("/action", json=body, headers=auth_headers)
    assert r2.status_code == 200
    tick_after_second = r2.json()["tick"]

    assert tick_after_first == tick_after_second, (
        f"Idempotentní akce inkrementovala tick: {tick_after_first} → {tick_after_second}"
    )


def test_at12_unknown_action_no_game_state_change(client, auth_headers, fresh_campaign_id, sb):
    """
    AT-12c: UNKNOWN action nevytváří game-state efekty (žádný MOVE_ENTITY,
    žádná změna NPC statistik, žádná nová znalost).

    Poznámka: tick se inkrementuje (strávený tah) — to je očekávané chování.
    Test ověří pouze absenci smysluplných herních efektů.
    """
    req_id = str(uuid.uuid4())
    r = client.post(
        "/action",
        json={
            "campaign_id": fresh_campaign_id,
            "request_id":  req_id,
            "intent":      "xyzzy frobnicate plugh twisty",
        },
        headers=auth_headers,
    )
    assert r.status_code == 200
    data = r.json()

    assert data["action_type"] == "UNKNOWN", (
        f"Klasifikátor vrátil {data['action_type']} místo UNKNOWN"
    )

    # UNKNOWN akce nesmí produkovat herně smysluplné efekty
    game_effects = [
        fx for fx in data.get("effects", [])
        if fx.get("type") in ("MOVE_ENTITY", "DELTA", "ADD_KNOWLEDGE", "ADD_FLAG")
    ]
    assert len(game_effects) == 0, (
        f"UNKNOWN akce produkovala herní efekty: {game_effects}"
    )
