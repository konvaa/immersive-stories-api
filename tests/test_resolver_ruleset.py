"""
tests/test_resolver_ruleset.py — Unit testy zapojení rulesetu do živého
resolveru (reconciliation sekce E + „Stále OTEVŘENÉ" blok, 2026-07-04).

Čisté unit testy [unit] — bez serveru, bez DB. Determinismus se řídí přes
request_id: build_rng_seed je čistá funkce, takže si pro daný world najdeme
request_id produkující požadovaný d20.

Spuštění: pytest tests/test_resolver_ruleset.py -v
"""

from __future__ import annotations

import random

import pytest

from engine.classifier import ActionType, classify
from engine.resolver import build_rng_seed, resolve
from engine.ruleset import (
    ADVENTURER_RULESET,
    NPC_INITIATIVE_SUSPICION,
    OutcomeTier,
)
from models.state import (
    EffectType,
    FrozenWorldView,
    LocationSnapshot,
    NPCSnapshot,
    PlayerSnapshot,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpery
# ---------------------------------------------------------------------------

def make_world(
    skills: dict[str, int] | None = None,
    world_pressure: int = 0,
    difficulty: str = "normal",
    player_hp: int = 20,
    player_flags: list[str] | None = None,
    player_knowledge: list[str] | None = None,
    npc_suspicion: int = 0,
    npc_hp: int = 20,
    ruleset_version: str = "adventurer-0.1.0",
) -> FrozenWorldView:
    player = PlayerSnapshot(
        player_id="u1", campaign_id="c1", location_id="crossroads_inn",
        hp=player_hp, max_hp=20, gold=10,
        flags=player_flags or [], knowledge=player_knowledge or [],
        skills=skills or {},
    )
    npc = NPCSnapshot(
        npc_id="npc-sheriff", campaign_id="c1", template_id="Adventurer",
        name="Sheriff", location_id="crossroads_inn",
        trust=30, fear=10, suspicion=npc_suspicion, hp=npc_hp,
    )
    loc = LocationSnapshot(
        row_id="loc-uuid", location_id="crossroads_inn", campaign_id="c1",
        name="Crossroads Inn", known_exits=["crossroads_exterior"],
    )
    return FrozenWorldView(
        campaign_id="c1", engine_version="1.0.0",
        ruleset_version=ruleset_version, seed=12345, tick=5,
        world_pressure=world_pressure, time_of_day="morning",
        difficulty=difficulty,
        player=player, npcs=(npc,), locations=(loc,),
    )


def request_id_for_roll(world: FrozenWorldView, target_roll: int) -> str:
    """Najde request_id, jehož derivovaný seed dá první d20 == target_roll."""
    for i in range(20_000):
        rid = f"req-{target_roll}-{i}"
        seed = build_rng_seed(
            world.seed, rid, world.tick,
            world.engine_version, world.ruleset_version,
        )
        if random.Random(seed).randint(1, 20) == target_roll:
            return rid
    raise AssertionError(f"request_id pro roll {target_roll} nenalezen")


def fx_of(res, fx_type, attribute=None):
    return [
        f for f in res.effects
        if f.type == fx_type and (attribute is None or f.attribute == attribute)
    ]


# ---------------------------------------------------------------------------
# Classifier: MANIPULATE + STEALTH
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("intent,expected", [
    ("sneak past the sheriff",        ActionType.STEALTH),
    ("hide behind the bar",           ActionType.STEALTH),
    ("creep up the stairs",           ActionType.STEALTH),
    ("slip past the guard",           ActionType.STEALTH),
    ("forge the merchant's ledger",   ActionType.MANIPULATE),
    ("tamper with the lock records",  ActionType.MANIPULATE),
    ("plant the dagger in his room",  ActionType.MANIPULATE),
    ("hide the evidence",             ActionType.MANIPULATE),
    ("cover my tracks",               ActionType.MANIPULATE),
    ("fake the signature",            ActionType.MANIPULATE),
    ("alter the record",              ActionType.MANIPULATE),
    # V3 regrese: víceznačná slova v ne-slovesné pozici NESMÍ být MANIPULATE
    ("look at the plant",             ActionType.OBSERVE),
    ("examine the fake document",     ActionType.INVESTIGATE),
    ("watch the doctor",              ActionType.OBSERVE),
    ("talk about the fake news",      ActionType.INFLUENCE),
    # regrese původních typů
    ("look around the room",          ActionType.OBSERVE),
    ("examine the fireplace",         ActionType.INVESTIGATE),
    ("talk to the innkeeper",         ActionType.INFLUENCE),
    ("attack the sheriff",            ActionType.ATTACK),
    ("xyzzy frobnicate",              ActionType.UNKNOWN),
])
def test_classifier(intent, expected):
    assert classify(intent) == expected


# ---------------------------------------------------------------------------
# DC z rulesetu + modifikátory
# ---------------------------------------------------------------------------

def test_dc_from_ruleset():
    world = make_world()
    res_inv = resolve(world, "INVESTIGATE", "search the room",
                      request_id_for_roll(world, 10))
    assert res_inv.dc == ADVENTURER_RULESET.action("INVESTIGATE").base_dc == 10
    res_inf = resolve(world, "INFLUENCE", "talk to sheriff",
                      request_id_for_roll(world, 10))
    assert res_inf.dc == 12
    assert res_inf.stakes == "MEDIUM"


def test_skill_bonus_applied():
    # awareness 50 → +2 (GDD 4.3)
    world = make_world(skills={"awareness": 50})
    rid = request_id_for_roll(world, 10)
    res = resolve(world, "INVESTIGATE", "search the room", rid)
    assert res.roll == 10
    assert res.total == 12, "awareness 50 má dát +2"
    assert res.margin == 2
    assert res.outcome == "CLEAN_SUCCESS"


def test_world_pressure_only_social():
    # pressure 7 → −3, ale JEN na sociální akce
    world = make_world(world_pressure=7)
    rid = request_id_for_roll(world, 10)
    res_social = resolve(world, "INFLUENCE", "convince the sheriff", rid)
    assert res_social.total == 7, "INFLUENCE má dostat world_pressure −3"
    res_phys = resolve(world, "INVESTIGATE", "search the room", rid)
    assert res_phys.total == 10, "INVESTIGATE nesmí být ovlivněn pressure"


def test_situational_knowledge_bonus():
    world = make_world(player_knowledge=["observed_crossroads_inn"])
    rid = request_id_for_roll(world, 10)
    res = resolve(world, "INVESTIGATE", "search the room", rid)
    assert res.total == 11, "observed_{loc} má dát +1 na INVESTIGATE"


# ---------------------------------------------------------------------------
# Natural 1 / 20 + difficulty prahy
# ---------------------------------------------------------------------------

def test_natural_one_catastrophic():
    world = make_world()
    res = resolve(world, "INVESTIGATE", "search the room",
                  request_id_for_roll(world, 1))
    assert res.outcome == "CATASTROPHIC_FAILURE"
    assert res.natural == 1
    assert not res.success


def test_natural_twenty_mitigated_under_dc():
    # ATTACK DC 14; nat 20 bez modifikátorů → margin +6 ≥ 0 → EXCEPTIONAL.
    # Pro mitigated potřebujeme DC > 20: INFLUENCE 12 + pressure −4 → efektivně 16...
    # margin = 20 − 4 − 12 = +4 ≥ 0 → pořád úspěch. Použijeme přímo resolve_outcome
    # scénář přes vysoké DC pomocí variant nelze — otestujeme aspoň EXCEPTIONAL.
    world = make_world()
    res = resolve(world, "ATTACK", "attack the sheriff",
                  request_id_for_roll(world, 20))
    assert res.outcome == "EXCEPTIONAL_SUCCESS"
    assert res.natural == 20
    assert not res.mitigated


def test_difficulty_profiles_change_tier():
    # roll 11, INVESTIGATE DC 10 → margin +1:
    # normal → SMALL_SUCCESS; insane (SMALL od +3, FAILURE od +1) → FAILURE
    w_normal = make_world(difficulty="normal")
    rid = request_id_for_roll(w_normal, 11)
    assert resolve(w_normal, "INVESTIGATE", "search", rid).outcome == "SMALL_SUCCESS"
    w_insane = make_world(difficulty="insane")
    assert resolve(w_insane, "INVESTIGATE", "search", rid).outcome == "FAILURE"


# ---------------------------------------------------------------------------
# Efekty: STEALTH / MANIPULATE
# ---------------------------------------------------------------------------

def test_stealth_success_hidden_flag():
    world = make_world(skills={"finesse": 75})   # +3; STEALTH DC 12
    rid = request_id_for_roll(world, 15)         # total 18, margin +6 → CLEAN
    res = resolve(world, "STEALTH", "sneak past the sheriff", rid)
    assert res.success
    flags = fx_of(res, EffectType.ADD_FLAG)
    assert any(f.flag == "hidden" for f in flags)


def test_stealth_failure_suspicion():
    world = make_world(player_flags=["hidden"])
    rid = request_id_for_roll(world, 4)          # total 4 vs DC 12 → HARD/SEVERE
    res = resolve(world, "STEALTH", "sneak past the sheriff", rid)
    assert not res.success
    assert any(f.flag == "hidden" for f in fx_of(res, EffectType.REMOVE_FLAG))
    susp = fx_of(res, EffectType.DELTA, "suspicion")
    assert susp and susp[0].value > 0


def test_manipulate_success_effects():
    world = make_world(skills={"presence": 50})  # +2; MANIPULATE DC 8
    rid = request_id_for_roll(world, 12)         # total 14, margin +6 → CLEAN
    res = resolve(world, "MANIPULATE", "forge the ledger", rid)
    assert res.success
    know = fx_of(res, EffectType.ADD_KNOWLEDGE)
    assert any(f.knowledge == "manipulated_crossroads_inn" for f in know)
    assert any(f.flag == "tampered" and f.target_id == "crossroads_inn"
               for f in fx_of(res, EffectType.ADD_FLAG))


def test_manipulate_failure_suspicion():
    world = make_world()
    rid = request_id_for_roll(world, 3)          # total 3 vs DC 8 → SEVERE/HARD
    res = resolve(world, "MANIPULATE", "forge the ledger", rid)
    assert not res.success
    susp = fx_of(res, EffectType.DELTA, "suspicion")
    assert susp and susp[0].value >= 10


# ---------------------------------------------------------------------------
# Combat: ATTACK, protiútok, hp zero, NPC iniciativa
# ---------------------------------------------------------------------------

def test_attack_success_damage_npc():
    world = make_world(skills={"finesse": 50})   # +2; ATTACK DC 14
    rid = request_id_for_roll(world, 18)         # total 20, margin +6 → CLEAN
    res = resolve(world, "ATTACK", "attack the sheriff", rid)
    assert res.success
    dmg = [f for f in fx_of(res, EffectType.DELTA, "hp")
           if f.target_id == "npc-sheriff"]
    assert dmg and dmg[0].value < 0, "úspěšný útok musí ubrat HP NPC"
    assert fx_of(res, EffectType.DELTA, "fear")
    assert fx_of(res, EffectType.DELTA, "suspicion")


def test_attack_failure_no_npc_damage_possible_counter():
    world = make_world()
    rid = request_id_for_roll(world, 3)          # total 3 vs DC 14 → HARD_FAILURE
    res = resolve(world, "ATTACK", "attack the sheriff", rid)
    assert not res.success
    npc_dmg = [f for f in fx_of(res, EffectType.DELTA, "hp")
               if f.target_id == "npc-sheriff"]
    assert not npc_dmg, "neúspěšný útok nesmí ubrat HP NPC"
    # Protiútok je hozen ze stejného rng — jen ověř konzistenci typů
    player_dmg = [f for f in fx_of(res, EffectType.DELTA, "hp")
                  if f.target_id == "u1"]
    for f in player_dmg:
        assert f.value < 0


def test_hp_zero_knocked_out_vs_game_over():
    def run(difficulty: str):
        # hp 1 → jakýkoli protiútok srazí na 0; hledáme kombinaci, kde protiútok padl
        world = make_world(player_hp=1, difficulty=difficulty)
        for roll in (2, 3, 4, 5, 6):
            for i in range(3000):
                rid = f"ko-{difficulty}-{roll}-{i}"
                seed = build_rng_seed(world.seed, rid, world.tick,
                                      world.engine_version, world.ruleset_version)
                if random.Random(seed).randint(1, 20) != roll:
                    continue
                res = resolve(world, "ATTACK", "attack the sheriff", rid)
                if any(f.type == EffectType.DELTA and f.attribute == "hp"
                       and f.target_id == "u1" for f in res.effects):
                    return res
        raise AssertionError("nepodařilo se vyvolat protiútok")

    res_normal = run("normal")
    assert any(f.type == EffectType.ADD_FLAG and f.flag == "knocked_out"
               for f in res_normal.effects), "normal → knocked_out"
    assert any(f.type == EffectType.SET and f.attribute == "hp" and f.value == 1
               for f in res_normal.effects), "knocked_out → HP reset na 1"

    res_hard = run("hard")
    assert any(f.type == EffectType.ADD_FLAG and f.flag == "game_over"
               for f in res_hard.effects), "hard → game_over"


def test_npc_initiative_at_high_suspicion():
    # Sheriff can_initiate=True; suspicion nad prahem → smí zaútočit na ne-ATTACK akci
    world = make_world(npc_suspicion=NPC_INITIATIVE_SUSPICION)
    hit = False
    for i in range(300):
        res = resolve(world, "OBSERVE", "look around", f"init-{i}")
        if any(f.type == EffectType.DELTA and f.attribute == "hp"
               and f.target_id == "u1" and f.value < 0 for f in res.effects):
            hit = True
            break
    assert hit, "Sheriff s suspicion ≥ prahu musí občas iniciovat útok"

    # Pod prahem NIKDY neiniciuje
    world_calm = make_world(npc_suspicion=NPC_INITIATIVE_SUSPICION - 1)
    for i in range(100):
        res = resolve(world_calm, "OBSERVE", "look around", f"calm-{i}")
        assert not any(f.type == EffectType.DELTA and f.attribute == "hp"
                       and f.target_id == "u1" for f in res.effects)


# ---------------------------------------------------------------------------
# Regrese: MOVE, UNKNOWN, determinismus, immutabilita
# ---------------------------------------------------------------------------

def test_move_invalid_destination():
    world = make_world()
    res = resolve(world, "MOVE", "go to xyzzy_nonexistent", "req-move-1")
    assert not res.success
    assert res.failure_reason and "INVALID_DESTINATION" in res.failure_reason
    assert not fx_of(res, EffectType.MOVE_ENTITY)


def test_move_valid_destination():
    world = make_world()
    rid = request_id_for_roll(world, 15)   # MOVE DC 2 → úspěch
    res = resolve(world, "MOVE", "go to the exterior", rid)
    assert res.success
    moves = fx_of(res, EffectType.MOVE_ENTITY)
    assert moves and moves[0].destination == "crossroads_exterior"


def test_unknown_no_effects():
    world = make_world()
    res = resolve(world, "UNKNOWN", "xyzzy frobnicate", "req-unk-1")
    assert res.effects == []


def test_unknown_no_effects_even_with_hostile_npc():
    """Audit K3 / AT-12c: UNKNOWN nesmí spustit NPC iniciativu ani žádný
    mechanický efekt — ani když je Sheriff nad prahem iniciativy."""
    world = make_world(npc_suspicion=100)
    for i in range(300):
        res = resolve(world, "UNKNOWN", "xyzzy frobnicate", f"unk-hostile-{i}")
        assert res.effects == [], (
            f"UNKNOWN vyprodukoval efekty: "
            f"{[(e.type.value, e.attribute, e.value) for e in res.effects]}"
        )


# ---------------------------------------------------------------------------
# Audit K5: game_over guard + mrtvá NPC
# ---------------------------------------------------------------------------

def test_is_game_over():
    from engine.resolver import is_game_over
    assert not is_game_over(make_world().player)
    assert is_game_over(make_world(player_flags=["game_over"]).player)


def test_dead_npc_no_initiative():
    """NPC s hp<=0 nesmí iniciovat útok, ani při suspicion 100."""
    world = make_world(npc_suspicion=100, npc_hp=0)
    for i in range(300):
        res = resolve(world, "OBSERVE", "look around", f"dead-init-{i}")
        assert not any(
            f.type == EffectType.DELTA and f.target_id == "u1"
            for f in res.effects
        ), "mrtvý Sheriff zaútočil"


def test_dead_npc_not_combat_or_social_target():
    """NPC s hp<=0 není cíl ATTACKu ani sociálních delt."""
    world = make_world(npc_hp=0)
    for i in range(50):
        res_atk = resolve(world, "ATTACK", "attack the sheriff", f"dead-atk-{i}")
        assert not any(f.target_id == "npc-sheriff" for f in res_atk.effects), \
            "combat efekt na mrtvé NPC"
        res_inf = resolve(world, "INFLUENCE", "talk to the sheriff", f"dead-inf-{i}")
        assert not any(f.target_id == "npc-sheriff" for f in res_inf.effects), \
            "sociální efekt na mrtvé NPC"


def test_determinism_same_request_id():
    world = make_world(skills={"awareness": 50})
    r1 = resolve(world, "INVESTIGATE", "search the room", "req-det-1")
    r2 = resolve(world, "INVESTIGATE", "search the room", "req-det-1")
    assert r1.roll == r2.roll and r1.outcome == r2.outcome
    assert [f.model_dump() for f in r1.effects] == [f.model_dump() for f in r2.effects]


def test_world_not_mutated():
    world = make_world(npc_suspicion=90)
    hp_before = world.player.hp
    susp_before = world.npcs[0].suspicion
    resolve(world, "ATTACK", "attack the sheriff", "req-mut-1")
    assert world.player.hp == hp_before
    assert world.npcs[0].suspicion == susp_before


def test_legacy_ruleset_version_fallback():
    # Kampaň s '1.0.0' (před migrací) musí fungovat — fallback na ADVENTURER_RULESET
    world = make_world(ruleset_version="1.0.0")
    res = resolve(world, "INVESTIGATE", "search the room",
                  request_id_for_roll(world, 10))
    assert res.dc == 10
    assert res.outcome in {t.name for t in OutcomeTier}
