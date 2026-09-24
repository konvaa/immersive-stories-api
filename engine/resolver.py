"""
engine/resolver.py — Hlavní resolúční smyčka herního enginu.

Od 2026-07-04 zapojen na engine/ruleset.py (reconciliation sekce E + „Stále
OTEVŘENÉ" blok):
  - DC z ActionDefinition rulesetu (get_ruleset(world.ruleset_version))
  - modifikátory: skill bonus (0–100 → +0..+4) + world_pressure (jen sociální
    akce s affected_by_pressure) + situační (znalost lokace)
  - outcome přes resolve_outcome(): 8 tierů, natural 1/20, margin prahy
    dle DIFFICULTY_PROFILES[world.difficulty]
  - stakes z ActionDefinition (narativní strop důsledku)
  - combat: ATTACK → melee_damage() na NPC; protiútok NPC při selhání;
    NPC iniciativa (can_initiate + suspicion ≥ prah) jako world event;
    0 HP hráče → resolve_hp_zero() (knocked_out / game_over flag)

Deterministika (GDD 20.4): rng_seed = md5(seed+request_id+tick+engine_version
+ruleset_version). PRVNÍ randint(1,20) z rng je vždy hráčův d20 — replay
(AT-10) rekonstruuje roll z uloženého rng_seed. Další hody (damage, protiútok)
čerpají ze STEJNÉHO rng ve fixním pořadí.

Efekty dle action_type (deltas škálované outcome tierem — zjednodušená
Interaction Pressure, sekce E bod 5):
  INFLUENCE   → trust/suspicion NPC v lokaci
  OBSERVE     → ADD_KNOWLEDGE "observed_{loc}" (jen úspěch)
  INVESTIGATE → ADD_KNOWLEDGE "investigated_{loc}"; selhání → suspicion+
  MANIPULATE  → ADD_KNOWLEDGE "manipulated_{loc}" + location flag "tampered";
                selhání → suspicion+
  STEALTH     → ADD_FLAG "hidden" hráči; selhání → REMOVE_FLAG + suspicion+
  MOVE        → MOVE_ENTITY (known_exits; jinak INVALID_DESTINATION)
  WAIT        → SET world.time_of_day (čas plyne i při selhání)
  ATTACK      → melee_damage na NPC + fear/suspicion; selhání → protiútok
  INTERACT    → ADD_KNOWLEDGE "interacted_{loc}" (jen úspěch)
  UNKNOWN     → žádné efekty (AT-12c)
"""

from __future__ import annotations

import hashlib
import random

from engine.classifier import ActionType
from engine.ruleset import (
    ACTION_SKILL_MAP,
    DIFFICULTY_NORMAL,
    DIFFICULTY_PROFILES,
    NPC_INITIATIVE_SUSPICION,
    OutcomeTier,
    Resolution,
    get_ruleset,
    melee_damage,
    npc_combat_profile,
    player_weapon,
    resolve_hp_zero,
    resolve_outcome,
    skill_level_bonus,
    world_pressure_modifier,
)
from models.state import (
    Effect, EffectType, FrozenWorldView, LocationSnapshot,
    NPCSnapshot, PlayerSnapshot, ResolutionOutput,
)

_TIME_PROGRESSION = ["morning", "noon", "afternoon", "evening", "night"]


def _next_time_of_day(current: str) -> str:
    idx = _TIME_PROGRESSION.index(current) if current in _TIME_PROGRESSION else 0
    return _TIME_PROGRESSION[(idx + 1) % len(_TIME_PROGRESSION)]


def _living_npcs_in_location(world: FrozenWorldView, location_id: str) -> list[NPCSnapshot]:
    """
    NPC v lokaci s hp > 0 (audit K5): mrtvé NPC není cíl combat/social efektů
    ani iniciativy. Pořadí je stabilní — loader řadí ORDER BY created_at, id.
    """
    return [n for n in world.npcs if n.location_id == location_id and n.hp > 0]


def is_game_over(player: PlayerSnapshot) -> bool:
    """Audit K5: hráč s game_over flagem nesmí pokračovat (guard v /action)."""
    return "game_over" in player.flags


def _find_move_target(intent: str, location: LocationSnapshot | None) -> str | None:
    """
    Hledá known_exit zmíněný v intentu.

    Dle GDD 12.3: pokud cíl není v known_exits, vrací None → INVALID_DESTINATION.
    ŽÁDNÝ fallback na první dostupný exit — engine rozhoduje, LLM jen narativně
    vysvětlí, proč pokus selhal.
    """
    if location is None:
        return None
    intent_lower = intent.lower()
    for exit_key in location.known_exits:
        readable = exit_key.lower().replace("_", " ")
        if readable in intent_lower or exit_key.lower() in intent_lower:
            return exit_key
    # Částečná shoda: aspoň jedno slovo z exit_key v intentu (např. 'jdi ven' ne,
    # ale 'go to the exterior' → crossroads_exterior ano)
    for exit_key in location.known_exits:
        words = exit_key.lower().split("_")
        if any(w in intent_lower for w in words if len(w) > 3):
            return exit_key
    return None


def build_rng_seed(
    campaign_seed: int,
    request_id: str,
    tick: int,
    engine_version: str,
    ruleset_version: str,
) -> int:
    """
    Deterministický seed dle GDD 20.4 — JEDINÝ zdroj pravdy pro derivaci.
    rng_seed = hash(campaign.seed + request_id + tick + engine_version
                    + ruleset_version)
    Replay používá ULOŽENÝ rng_seed z events — nikdy ho neregeneruje.
    """
    raw = (
        f"{campaign_seed}{request_id}{tick}{engine_version}{ruleset_version}"
    ).encode()
    return int(hashlib.md5(raw).hexdigest(), 16) % (2 ** 31)


# ---------------------------------------------------------------------------
# Modifikátory hodu
# ---------------------------------------------------------------------------

def _compute_modifiers(
    world: FrozenWorldView,
    action_type: str,
    skill_name: str | None,
    affected_by_pressure: bool,
) -> int:
    """
    Součet modifikátorů (GDD 4.3, zjednodušená první verze):
      skill bonus      +0..+4 dle player.skills[skill] (0–100)
      world_pressure   0..−4, POUZE sociální akce (affected_by_pressure)
      situační znalost +1 — znalost lokace pomáhá navazující akci:
                       INVESTIGATE po observed_{loc}, MANIPULATE po investigated_{loc}
    Mentor/equipment/fatigue přijdou s plným templatem (RollModifiers je má).
    """
    player = world.player
    mods = 0
    if skill_name:
        mods += skill_level_bonus(player.skills.get(skill_name, 0))
    if affected_by_pressure:
        mods += world_pressure_modifier(world.world_pressure)
    loc = player.location_id
    if action_type == ActionType.INVESTIGATE and f"observed_{loc}" in player.knowledge:
        mods += 1
    elif action_type == ActionType.MANIPULATE and f"investigated_{loc}" in player.knowledge:
        mods += 1
    return mods


# ---------------------------------------------------------------------------
# Škálování sociálních delt dle tieru (zjednodušená Interaction Pressure)
# ---------------------------------------------------------------------------

# tier → (trust_delta, suspicion_delta) pro INFLUENCE
_INFLUENCE_DELTAS: dict[OutcomeTier, tuple[int, int]] = {
    OutcomeTier.EXCEPTIONAL_SUCCESS:  (20, -10),
    OutcomeTier.CLEAN_SUCCESS_BONUS:  (20, -10),
    OutcomeTier.CLEAN_SUCCESS:        (10, -5),
    OutcomeTier.SMALL_SUCCESS:        (5,   0),
    OutcomeTier.FAILURE:              (-5,  10),
    OutcomeTier.SEVERE_FAILURE:       (-10, 15),
    OutcomeTier.HARD_FAILURE:         (-15, 20),
    OutcomeTier.CATASTROPHIC_FAILURE: (-15, 20),
}

# tier selhání → suspicion delta pro INVESTIGATE / MANIPULATE / STEALTH
_FAILURE_SUSPICION: dict[OutcomeTier, int] = {
    OutcomeTier.FAILURE:              10,
    OutcomeTier.SEVERE_FAILURE:       15,
    OutcomeTier.HARD_FAILURE:         20,
    OutcomeTier.CATASTROPHIC_FAILURE: 20,
}


def _failure_suspicion(res: Resolution) -> int:
    """Suspicion přírůstek při selhání; mitigated (nat 20) = poloviční dopad."""
    base = _FAILURE_SUSPICION.get(res.tier, 10)
    return base // 2 if res.mitigated else base


# ---------------------------------------------------------------------------
# Hlavní resolve
# ---------------------------------------------------------------------------

def resolve(
    world: FrozenWorldView,
    action_type: str,
    intent: str,
    request_id: str,
) -> ResolutionOutput:
    """
    Hlavní entry point resolveru.

    Args:
        world:       Zmrazený stav světa.
        action_type: Výstup klasifikátoru (ActionType.value).
        intent:      Původní text akce hráče.
        request_id:  UUID requestu (pro rng_seed).

    Returns:
        ResolutionOutput s efekty, výsledkem a metadaty hodu.
    """
    ruleset = get_ruleset(world.ruleset_version)
    action_def = ruleset.action(action_type)
    profile = DIFFICULTY_PROFILES.get(world.difficulty, DIFFICULTY_NORMAL)

    dc = action_def.base_dc
    skill_name = action_def.skill or ACTION_SKILL_MAP.get(action_type)
    modifiers = _compute_modifiers(
        world, action_type, skill_name, action_def.affected_by_pressure,
    )

    rng_seed = build_rng_seed(
        world.seed, request_id, world.tick,
        world.engine_version, world.ruleset_version,
    )
    rng = random.Random(rng_seed)
    roll = rng.randint(1, 20)   # PRVNÍ hod z rng = hráčův d20 (replay kontrakt)

    res = resolve_outcome(roll, modifiers, dc, action_def.base_stakes, profile)
    tier = res.tier
    success = res.is_success

    effects: list[Effect] = []
    player: PlayerSnapshot = world.player
    player_loc_id = player.location_id
    player_id = player.player_id
    invalid_destination = False
    combat_notes: list[str] = []
    player_hp_delta = 0   # projekce pro hp-zero handling

    # -----------------------------------------------------------------------
    # INFLUENCE — trust/suspicion NPC v lokaci, škálované tierem
    # -----------------------------------------------------------------------
    if action_type == ActionType.INFLUENCE:
        trust_d, susp_d = _INFLUENCE_DELTAS[tier]
        if res.mitigated:
            trust_d, susp_d = 0, susp_d // 2   # nat 20: bez vážného následku
        for npc in _living_npcs_in_location(world, player_loc_id):
            if trust_d:
                effects.append(Effect(type=EffectType.DELTA, target_id=npc.npc_id,
                                      attribute="trust", value=trust_d))
            if susp_d:
                effects.append(Effect(type=EffectType.DELTA, target_id=npc.npc_id,
                                      attribute="suspicion", value=susp_d))

    # -----------------------------------------------------------------------
    # OBSERVE — znalost lokace (jen při úspěchu; DC 2 → skoro vždy)
    # -----------------------------------------------------------------------
    elif action_type == ActionType.OBSERVE:
        if success:
            effects.append(Effect(
                type=EffectType.ADD_KNOWLEDGE, target_id=player_id,
                knowledge=f"observed_{player_loc_id}",
            ))

    # -----------------------------------------------------------------------
    # INVESTIGATE — znalost při úspěchu; selhání → suspicion NPC
    # -----------------------------------------------------------------------
    elif action_type == ActionType.INVESTIGATE:
        if success:
            effects.append(Effect(
                type=EffectType.ADD_KNOWLEDGE, target_id=player_id,
                knowledge=f"investigated_{player_loc_id}",
            ))
            # TODO: CLEAN_SUCCESS_BONUS+ → DISCOVER_EXIT z hidden_exits,
            # až effect_applier + writer umí known_exits update.
        else:
            for npc in _living_npcs_in_location(world, player_loc_id):
                effects.append(Effect(
                    type=EffectType.DELTA, target_id=npc.npc_id,
                    attribute="suspicion", value=_failure_suspicion(res) // 2,
                ))

    # -----------------------------------------------------------------------
    # MANIPULATE — změna faktů/skrytého stavu (GDD reconciliation, 2. dávka)
    # -----------------------------------------------------------------------
    elif action_type == ActionType.MANIPULATE:
        if success:
            effects.append(Effect(
                type=EffectType.ADD_KNOWLEDGE, target_id=player_id,
                knowledge=f"manipulated_{player_loc_id}",
            ))
            effects.append(Effect(
                type=EffectType.ADD_FLAG, target_id=player_loc_id,
                flag="tampered",
            ))
        else:
            for npc in _living_npcs_in_location(world, player_loc_id):
                effects.append(Effect(
                    type=EffectType.DELTA, target_id=npc.npc_id,
                    attribute="suspicion", value=_failure_suspicion(res),
                ))

    # -----------------------------------------------------------------------
    # STEALTH — hidden flag; prozrazení → suspicion
    # -----------------------------------------------------------------------
    elif action_type == ActionType.STEALTH:
        if success:
            effects.append(Effect(
                type=EffectType.ADD_FLAG, target_id=player_id, flag="hidden",
            ))
            if tier >= OutcomeTier.CLEAN_SUCCESS_BONUS:
                for npc in _living_npcs_in_location(world, player_loc_id):
                    effects.append(Effect(
                        type=EffectType.DELTA, target_id=npc.npc_id,
                        attribute="suspicion", value=-5,
                    ))
        else:
            if "hidden" in player.flags:
                effects.append(Effect(
                    type=EffectType.REMOVE_FLAG, target_id=player_id, flag="hidden",
                ))
            for npc in _living_npcs_in_location(world, player_loc_id):
                effects.append(Effect(
                    type=EffectType.DELTA, target_id=npc.npc_id,
                    attribute="suspicion", value=_failure_suspicion(res),
                ))

    # -----------------------------------------------------------------------
    # MOVE — přesun (known_exits; jinak INVALID_DESTINATION)
    # -----------------------------------------------------------------------
    elif action_type == ActionType.MOVE:
        location = next((l for l in world.locations if l.location_id == player_loc_id), None)
        target = _find_move_target(intent, location)
        if target is None:
            # GDD 12.3: cíl není v known_exits → INVALID_DESTINATION, žádný přesun
            tier = OutcomeTier.FAILURE
            success = False
            invalid_destination = True
        elif success:
            effects.append(Effect(
                type=EffectType.MOVE_ENTITY, target_id=player_id,
                destination=target,
            ))

    # -----------------------------------------------------------------------
    # WAIT — čas plyne vždy (i nat 1 znamená jen nepohodlné čekání; Stakes LOW)
    # -----------------------------------------------------------------------
    elif action_type == ActionType.WAIT:
        effects.append(Effect(
            type=EffectType.SET, target_id="world",
            attribute="time_of_day",
            value=_next_time_of_day(world.time_of_day),
        ))

    # -----------------------------------------------------------------------
    # ATTACK — melee_damage na NPC; selhání → protiútok NPC
    # -----------------------------------------------------------------------
    elif action_type == ActionType.ATTACK:
        npcs = _living_npcs_in_location(world, player_loc_id)
        if npcs:
            target_npc = npcs[0]   # TODO: target parsing z intentu (ActionAttempt vrstva)
            # Pokus o útok vždy vyděsí a zalarmuje okolí
            effects += [
                Effect(type=EffectType.DELTA, target_id=target_npc.npc_id,
                       attribute="fear", value=15 if success else 10),
                Effect(type=EffectType.DELTA, target_id=target_npc.npc_id,
                       attribute="suspicion", value=30 if success else 20),
            ]
            if success:
                finesse = skill_level_bonus(player.skills.get("finesse", 0))
                dmg = melee_damage(
                    player_weapon(player.flags), finesse_bonus=finesse,
                    margin=res.margin, armor=0, rng=rng,
                )
                if dmg > 0:
                    effects.append(Effect(
                        type=EffectType.DELTA, target_id=target_npc.npc_id,
                        attribute="hp", value=-dmg,
                    ))
                combat_notes.append(f"zásah {target_npc.name} za {dmg}")
            elif not res.mitigated:
                # Protiútok bránícího se NPC (reakce — nezávisí na can_initiate)
                prof = npc_combat_profile(target_npc.name)
                counter_roll = rng.randint(1, 20)
                if counter_roll >= prof.attack_dc:
                    counter_dmg = prof.damage_dice.roll(rng)
                    player_hp_delta -= counter_dmg
                    effects.append(Effect(
                        type=EffectType.DELTA, target_id=player_id,
                        attribute="hp", value=-counter_dmg,
                    ))
                    combat_notes.append(
                        f"protiútok {target_npc.name} za {counter_dmg}"
                    )

    # -----------------------------------------------------------------------
    # INTERACT — znalost interakce (jen při úspěchu)
    # -----------------------------------------------------------------------
    elif action_type == ActionType.INTERACT:
        if success:
            effects.append(Effect(
                type=EffectType.ADD_KNOWLEDGE, target_id=player_id,
                knowledge=f"interacted_{player_loc_id}",
            ))

    # UNKNOWN → žádné efekty (AT-12c)

    # -----------------------------------------------------------------------
    # NPC iniciativa — world event (can_initiate + suspicion ≥ prah).
    # Max jeden iniciátor za tick; přeskočeno pokud už proběhl protiútok.
    # UNKNOWN vyloučeno (audit K3): nenaparsovaný vstup musí být mechanicky
    # bezpečný (AT-12c) — narativ smí reagovat, stav světa ne.
    # -----------------------------------------------------------------------
    if (action_type not in (ActionType.ATTACK, ActionType.UNKNOWN)
            and player_hp_delta == 0):
        for npc in _living_npcs_in_location(world, player_loc_id):
            prof = npc_combat_profile(npc.name)
            if not (prof.can_initiate and npc.hp > 0
                    and npc.suspicion >= NPC_INITIATIVE_SUSPICION):
                continue
            init_roll = rng.randint(1, 20)
            if init_roll >= prof.attack_dc:
                init_dmg = prof.damage_dice.roll(rng)
                player_hp_delta -= init_dmg
                effects.append(Effect(
                    type=EffectType.DELTA, target_id=player_id,
                    attribute="hp", value=-init_dmg,
                ))
                combat_notes.append(f"{npc.name} zaútočil (iniciativa) za {init_dmg}")
            else:
                combat_notes.append(f"{npc.name} se pokusil zaútočit a minul")
            break   # jeden iniciátor za tick

    # -----------------------------------------------------------------------
    # 0 HP hráče → knocked_out / game_over dle difficulty (death_allowed)
    # -----------------------------------------------------------------------
    if player_hp_delta < 0 and (player.hp + player_hp_delta) <= 0:
        outcome_0hp = resolve_hp_zero(profile.death_allowed)
        if outcome_0hp == "knocked_out":
            effects += [
                Effect(type=EffectType.SET, target_id=player_id,
                       attribute="hp", value=1),
                Effect(type=EffectType.ADD_FLAG, target_id=player_id,
                       flag="knocked_out"),
            ]
        else:
            effects.append(Effect(
                type=EffectType.ADD_FLAG, target_id=player_id, flag="game_over",
            ))
        combat_notes.append(outcome_0hp)

    outcome = tier.name
    narrative_hint = (
        f"d20={roll}{modifiers:+d} = {res.total} vs DC {dc} "
        f"(margin {res.margin:+d}, stakes {res.stakes.value}, "
        f"difficulty {profile.name}) → {outcome}"
        f"{' [mitigated nat20]' if res.mitigated else ''} | "
        f"akce: {action_type} | efektů: {len(effects)}"
    )
    if combat_notes:
        narrative_hint += " | combat: " + "; ".join(combat_notes)

    if invalid_destination:
        failure_reason = (
            "INVALID_DESTINATION: cíl není mezi známými východy — "
            "hráč o této cestě neví nebo neexistuje."
        )
    elif success:
        failure_reason = None
    else:
        failure_reason = (
            f"Hod {roll}{modifiers:+d} = {res.total} nesplnil DC {dc} "
            f"(margin {res.margin:+d} → {outcome})."
        )

    return ResolutionOutput(
        effects=effects,
        outcome=outcome,
        roll=roll,
        total=res.total,
        dc=dc,
        margin=res.margin,
        stakes=res.stakes.value,
        mitigated=res.mitigated,
        natural=res.natural,
        narrative_hint=narrative_hint,
        success=success,
        failure_reason=failure_reason,
    )
