"""
engine/ruleset.py — Datový model rulesetu a resolution mechanika (GDD v0.8 4.1–4.6).

Samostatný, čistý modul BEZ DB/IO závislostí — snadno testovatelný a verzovatelný.
Zapojen do živého resolveru (engine/resolver.py) přes get_ruleset() — 2026-07-04.

Resolution systém (finalizováno 2026-06-13, GDD 4.4 + design diskuze):
  total  = d20 + součet modifikátorů
  margin = total - DC
  Natural 1  → vždy CATASTROPHIC_FAILURE (severitu stropuje stakes)
  Natural 20 → margin ≥ 0: EXCEPTIONAL_SUCCESS; margin < 0: mírné selhání bez
               vážného následku (FAILURE + mitigated=True)
  Jinak (roll 2–19): tier podle margin prahů v DifficultyProfile.

Default (medium) prahy reprodukují příklad DC 12:
  total 1–6 hard / 7–9 severe / 10–11 fail / 12–13 small / 14–18 clean / 19+ bonus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum


# ---------------------------------------------------------------------------
# Outcome tiers
# ---------------------------------------------------------------------------

class OutcomeTier(IntEnum):
    """Seřazené od nejhoršího po nejlepší (rank = pořadí)."""
    CATASTROPHIC_FAILURE = 0   # natural 1
    HARD_FAILURE         = 1
    SEVERE_FAILURE       = 2
    FAILURE              = 3
    SMALL_SUCCESS        = 4
    CLEAN_SUCCESS        = 5
    CLEAN_SUCCESS_BONUS  = 6
    EXCEPTIONAL_SUCCESS  = 7   # natural 20 při úspěchu

    @property
    def is_success(self) -> bool:
        return self >= OutcomeTier.SMALL_SUCCESS


class Stakes(str, Enum):
    """Strop narativního důsledku (GDD 4.6). Neovlivňuje DC ani margin —
    určuje, co konkrétně 'katastrofa' / 'serious cost' znamená."""
    LOW     = "LOW"
    MEDIUM  = "MEDIUM"
    HIGH    = "HIGH"
    EXTREME = "EXTREME"


# ---------------------------------------------------------------------------
# Skill level → bonus (GDD 4.3)
# ---------------------------------------------------------------------------

def skill_level_bonus(level: int) -> int:
    """Skill level 0–100 → modifikátor +0..+4 (GDD 4.3)."""
    if level >= 100:
        return 4
    if level >= 75:
        return 3
    if level >= 50:
        return 2
    if level >= 25:
        return 1
    return 0


# Obecné skilly (všechny světy), rozsah 0–100. Template světa může přidat vlastní
# (např. Drow: arcana, stealth, deception).
# Pozn.: `resolve` zatím není namapovaný na žádnou akci (C2) — počítá se s ním
# pro odolnostní checky / saving throws později.
CORE_SKILLS: tuple[str, ...] = ("awareness", "resolve", "presence", "finesse")

# Base mapování akce → skill (C2). Template může přepsat. None = akce bez skill bonusu.
# Pozn.: MANIPULATE zatím NENÍ v engine/classifier.py ActionType — nutno doplnit
# klasifikátor, jinak se akce nikdy nenaklasifikuje.
ACTION_SKILL_MAP: dict[str, str | None] = {
    "OBSERVE":     "awareness",
    "INVESTIGATE": "awareness",
    "INFLUENCE":   "presence",
    "ATTACK":      "finesse",
    "MOVE":        "finesse",
    "WAIT":        None,
    "INTERACT":    "finesse",
    "MANIPULATE":  "presence",   # GDD: změna známých faktů / skrytého stavu (ne osoby/objekt)
    "STEALTH":     "finesse",
}


def world_pressure_modifier(world_pressure: int) -> int:
    """
    Penalizace hodu z napětí ve světě (roste s ticky). Aplikuje se POUZE na
    sociální akce (INFLUENCE, MANIPULATE) — při napětí jsou NPC ostražitější
    a nedůvěřivější. NEovlivňuje fyzické akce (lockpick je lockpick); rozdíly
    fyzického prostředí (dřevo vs ocel) řeší base_dc v ActionDefinition.
    """
    if world_pressure >= 10:
        return -4
    if world_pressure >= 7:
        return -3
    if world_pressure >= 5:
        return -2
    if world_pressure >= 3:
        return -1
    return 0


@dataclass(frozen=True)
class RollModifiers:
    """Situační modifikátory hodu (GDD 4.3). Sčítají se do total = d20 + total()."""
    skill: int = 0          # ze skill_level_bonus()
    knowledge: int = 0      # +1..+3 relevantní knowledge flags
    mentor: int = 0         # +1..+3 trainer/mentor přítomen
    equipment: int = 0      # +1..+2 vybavení
    fatigue: int = 0        # -1..-3 únava/zranění (záporné)
    environment: int = 0    # -3..+3 prostředí (zahrnuje world_pressure)

    def total(self) -> int:
        return (self.skill + self.knowledge + self.mentor
                + self.equipment + self.fatigue + self.environment)


# ---------------------------------------------------------------------------
# Difficulty profile — margin prahy (konfigurovatelné per obtížnost světa)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DifficultyProfile:
    """
    Difficulty tier — mapuje margin (total - DC) na OutcomeTier + řídí smrt.
    `breakpoints` je seznam (min_margin, tier) seřazený SESTUPNĚ; první práh,
    kterému margin vyhoví (margin >= min_margin), vyhrává.
    `death_allowed`: smí hráč při 0 HP zemřít (game over), nebo jen knocked_out?

    4 tiery (2026-06-13): story / normal / hard / insane.
    """
    name: str
    breakpoints: tuple[tuple[int, OutcomeTier], ...]
    death_allowed: bool = False

    def tier_for_margin(self, margin: int) -> OutcomeTier:
        for min_margin, tier in self.breakpoints:
            if margin >= min_margin:
                return tier
        return OutcomeTier.HARD_FAILURE  # bezpečnostní fallback


# story — smrt nemožná, následky čistě narativní; mírné prahy.
DIFFICULTY_STORY = DifficultyProfile(
    name="story", death_allowed=False,
    breakpoints=(
        (5,  OutcomeTier.CLEAN_SUCCESS_BONUS),
        (1,  OutcomeTier.CLEAN_SUCCESS),
        (-2, OutcomeTier.SMALL_SUCCESS),
        (-4, OutcomeTier.FAILURE),
        (-7, OutcomeTier.SEVERE_FAILURE),
        (-999, OutcomeTier.HARD_FAILURE),
    ),
)
# normal — smrt nemožná, ale vážné následky (captured/injured/setback).
# Prahy OVĚŘENY: přesně reprodukují příklad DC 12.
DIFFICULTY_NORMAL = DifficultyProfile(
    name="normal", death_allowed=False,
    breakpoints=(
        (7,  OutcomeTier.CLEAN_SUCCESS_BONUS),
        (2,  OutcomeTier.CLEAN_SUCCESS),
        (0,  OutcomeTier.SMALL_SUCCESS),
        (-2, OutcomeTier.FAILURE),
        (-5, OutcomeTier.SEVERE_FAILURE),
        (-999, OutcomeTier.HARD_FAILURE),
    ),
)
# hard — smrt možná; přísnější prahy. (hodnoty k doladění)
DIFFICULTY_HARD = DifficultyProfile(
    name="hard", death_allowed=True,
    breakpoints=(
        (9,  OutcomeTier.CLEAN_SUCCESS_BONUS),
        (4,  OutcomeTier.CLEAN_SUCCESS),
        (2,  OutcomeTier.SMALL_SUCCESS),
        (0,  OutcomeTier.FAILURE),
        (-3, OutcomeTier.SEVERE_FAILURE),
        (-999, OutcomeTier.HARD_FAILURE),
    ),
)
# insane — smrt možná, vyšší stakes ceiling; nejpřísnější prahy. (k doladění)
DIFFICULTY_INSANE = DifficultyProfile(
    name="insane", death_allowed=True,
    breakpoints=(
        (10, OutcomeTier.CLEAN_SUCCESS_BONUS),
        (6,  OutcomeTier.CLEAN_SUCCESS),
        (3,  OutcomeTier.SMALL_SUCCESS),
        (1,  OutcomeTier.FAILURE),
        (-2, OutcomeTier.SEVERE_FAILURE),
        (-999, OutcomeTier.HARD_FAILURE),
    ),
)

DIFFICULTY_PROFILES: dict[str, DifficultyProfile] = {
    p.name: p for p in (DIFFICULTY_STORY, DIFFICULTY_NORMAL, DIFFICULTY_HARD, DIFFICULTY_INSANE)
}


# ---------------------------------------------------------------------------
# Action definitions + ruleset config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionDefinition:
    """GDD 4.2 — definice akce v rulesetu.

    `variants` drží intensity profily (Addendum I, ActionAttempt vrstva — Phase 2):
    pojmenovaná varianta záměru → override DC. Base resolver je zatím ignoruje
    a používá base_dc; intent klasifikátor je vybere později.
    """
    action_type: str
    base_dc: int
    base_stakes: Stakes
    skill: str | None = None   # který skill se na akci váže; None = bez skill bonusu
    variants: dict[str, int] = field(default_factory=dict)
    affected_by_pressure: bool = False   # True → aplikovat world_pressure_modifier (sociální akce)


@dataclass(frozen=True)
class Ruleset:
    """Statická konfigurace pravidel (GDD 0.2 / Addendum B). Váže se přes version."""
    version: str
    difficulty: DifficultyProfile
    actions: dict[str, ActionDefinition]

    def action(self, action_type: str) -> ActionDefinition:
        return self.actions.get(
            action_type,
            ActionDefinition(action_type, base_dc=10, base_stakes=Stakes.MEDIUM),
        )


# --- Default Adventurer ruleset (Crossroads Inn) --------------------------
# base_dc / stakes dle D4 (2026-06-13). Reprezentativní DC per action_type;
# jemnější intenzity (careful, casual, pressure) jsou ve `variants` pro pozdější
# ActionAttempt vrstvu. Sociální akce (INFLUENCE, MANIPULATE) mají affected_by_pressure=True.
# MANIPULATE = změna faktů/skrytého stavu (přepsání dokumentu, falšování stopy) → nízké DC.
ADVENTURER_RULESET = Ruleset(
    version="adventurer-0.1.0",
    difficulty=DIFFICULTY_NORMAL,
    actions={
        "WAIT":        ActionDefinition("WAIT",        base_dc=0,  base_stakes=Stakes.LOW),
        "OBSERVE":     ActionDefinition("OBSERVE",     base_dc=2,  base_stakes=Stakes.LOW,    skill="awareness"),
        "MOVE":        ActionDefinition("MOVE",        base_dc=2,  base_stakes=Stakes.LOW,    skill="finesse"),
        "INTERACT":    ActionDefinition("INTERACT",    base_dc=8,  base_stakes=Stakes.LOW,    skill="finesse"),
        "STEALTH":     ActionDefinition("STEALTH",     base_dc=12, base_stakes=Stakes.MEDIUM, skill="finesse"),
        "INVESTIGATE": ActionDefinition("INVESTIGATE", base_dc=10, base_stakes=Stakes.MEDIUM, skill="awareness",
                                        variants={"careful": 14}),
        "INFLUENCE":   ActionDefinition("INFLUENCE",   base_dc=12, base_stakes=Stakes.MEDIUM, skill="presence",
                                        affected_by_pressure=True, variants={"casual": 4, "pressure": 15}),
        "MANIPULATE":  ActionDefinition("MANIPULATE",  base_dc=8,  base_stakes=Stakes.MEDIUM, skill="presence",
                                        affected_by_pressure=True),
        "ATTACK":      ActionDefinition("ATTACK",      base_dc=14, base_stakes=Stakes.HIGH,   skill="finesse"),
    },
)


# ---------------------------------------------------------------------------
# Combat / HP (Adventurer template; jiné světy mohou mít jiný/žádný HP systém)
# ---------------------------------------------------------------------------
# POZN.: HP je template-specific. Hodnoty zbraní jsou PŘÍKLAD pro mini Adventurer
# scénu — plný gear/combat systém přijde s full Adventurer templatem.

ADVENTURER_MAX_HP = 20   # menší čísla = čitelnější pro hráče než 100


@dataclass(frozen=True)
class DiceSpec:
    count: int
    sides: int
    flat: int = 0

    def roll(self, rng) -> int:
        """rng = objekt s .randint(a, b) (např. random.Random seedovaný resolverem)."""
        return sum(rng.randint(1, self.sides) for _ in range(self.count)) + self.flat

    def max(self) -> int:
        return self.count * self.sides + self.flat

    @classmethod
    def parse(cls, spec: str) -> "DiceSpec":
        """'1d8', '1d10+2', '2d6-1', '4' (plochá hodnota)."""
        spec = spec.strip().lower().replace(" ", "")
        flat = 0
        if "d" not in spec:
            return cls(0, 0, int(spec))
        dice, _, rest = spec.partition("d")
        count = int(dice or "1")
        if "+" in rest:
            sides_s, _, f = rest.partition("+"); flat = int(f)
        elif "-" in rest:
            sides_s, _, f = rest.partition("-"); flat = -int(f)
        else:
            sides_s = rest
        return cls(count, int(sides_s), flat)


# Příklad sady zbraní (k nahrazení full gear systémem).
ADVENTURER_WEAPONS: dict[str, DiceSpec] = {
    "unarmed":     DiceSpec(1, 4),
    "dagger":      DiceSpec(1, 6),
    "sword":       DiceSpec(1, 8),
    "greatweapon": DiceSpec(1, 10, 2),
}


def player_weapon(flags: list[str] | tuple[str, ...]) -> DiceSpec:
    """
    Zbraň hráče z player flags ('weapon:sword' → sword). Bez flagu → unarmed.
    Provizorní do full gear systému.
    """
    for f in flags:
        if f.startswith("weapon:"):
            return ADVENTURER_WEAPONS.get(f.split(":", 1)[1], ADVENTURER_WEAPONS["unarmed"])
    return ADVENTURER_WEAPONS["unarmed"]

# Margin práh, od kterého útok přidá bonusový 1d4 damage.
DAMAGE_BONUS_MARGIN = 7
_BONUS_DIE = DiceSpec(1, 4)


@dataclass(frozen=True)
class NPCCombatProfile:
    """Bojové parametry NPC v ruleset configu."""
    attack_dc: int
    damage_dice: DiceSpec
    can_initiate: bool = False   # smí NPC zaútočit samo (loupež, přepad, monstrum)


def melee_damage(weapon: DiceSpec, finesse_bonus: int, margin: int,
                 armor: int, rng) -> int:
    """
    Damage útoku: zbraň + finesse bonus (+0..+4) + bonus 1d4 při vysokém marginu,
    minus armor cíle. Nikdy záporné.
    """
    dmg = weapon.roll(rng) + finesse_bonus
    if margin >= DAMAGE_BONUS_MARGIN:
        dmg += _BONUS_DIE.roll(rng)
    return max(0, dmg - armor)


# --- NPC combat profily (Adventurer mini scéna; později template-driven) ------
# attack_dc = práh na d20, který NPC musí hodit, aby zasáhlo hráče (nižší = lepší bojovník).
DEFAULT_NPC_COMBAT = NPCCombatProfile(attack_dc=12, damage_dice=DiceSpec(1, 4))
ADVENTURER_NPC_COMBAT: dict[str, NPCCombatProfile] = {
    "sheriff":   NPCCombatProfile(attack_dc=10, damage_dice=DiceSpec(1, 6), can_initiate=True),
    "innkeeper": NPCCombatProfile(attack_dc=14, damage_dice=DiceSpec(1, 4)),
}

# NPC s can_initiate zaútočí samo (world event), když jeho suspicion dosáhne prahu.
NPC_INITIATIVE_SUSPICION = 80


def npc_combat_profile(name: str) -> NPCCombatProfile:
    """Combat profil NPC podle jména (lowercase). Fallback DEFAULT_NPC_COMBAT."""
    return ADVENTURER_NPC_COMBAT.get(name.strip().lower(), DEFAULT_NPC_COMBAT)


def resolve_hp_zero(death_allowed: bool) -> str:
    """
    Co se stane při 0 HP hráče.
      hard/insane (death_allowed) → 'game_over' (Flutter: "You have fallen", restart/load)
      story/normal                → 'knocked_out' (HP reset na 1 + narativní následek)
    """
    return "game_over" if death_allowed else "knocked_out"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    tier: OutcomeTier
    roll: int            # holý d20 (1–20)
    total: int           # roll + modifikátory
    dc: int
    margin: int          # total - dc
    stakes: Stakes
    mitigated: bool      # natural 20 změkčilo neúspěch (žádný vážný následek)
    natural: int | None  # 1 / 20 / None

    @property
    def is_success(self) -> bool:
        return self.tier.is_success


def resolve_outcome(
    roll: int,
    modifiers: int,
    dc: int,
    stakes: Stakes,
    profile: DifficultyProfile = DIFFICULTY_NORMAL,
) -> Resolution:
    """
    Spočítá výsledek skill checku. `roll` je holý d20 (1–20), `modifiers` jejich součet.

    Natural 1  → CATASTROPHIC_FAILURE (vždy).
    Natural 20 → EXCEPTIONAL_SUCCESS pokud by margin ≥ 0, jinak FAILURE + mitigated.
    Jinak      → tier dle margin prahů profilu.
    """
    total = roll + modifiers
    margin = total - dc
    mitigated = False
    natural: int | None = None

    if roll == 1:
        natural = 1
        tier = OutcomeTier.CATASTROPHIC_FAILURE
    elif roll == 20:
        natural = 20
        if margin >= 0:
            tier = OutcomeTier.EXCEPTIONAL_SUCCESS
        else:
            tier = OutcomeTier.FAILURE  # nejmírnější selhání
            mitigated = True
    else:
        tier = profile.tier_for_margin(margin)

    return Resolution(
        tier=tier, roll=roll, total=total, dc=dc, margin=margin,
        stakes=stakes, mitigated=mitigated, natural=natural,
    )


# ---------------------------------------------------------------------------
# Ruleset registry
# ---------------------------------------------------------------------------

RULESETS: dict[str, Ruleset] = {
    ADVENTURER_RULESET.version: ADVENTURER_RULESET,
}


def get_ruleset(version: str) -> Ruleset:
    """
    Ruleset pro danou verzi. Legacy kampaně ('1.0.0') i neznámé verze
    dostanou ADVENTURER_RULESET — jediný existující ruleset.
    Pozn.: rng_seed používá ULOŽENOU campaigns.ruleset_version, takže fallback
    nemění determinismus derivace seedu.
    """
    return RULESETS.get(version, ADVENTURER_RULESET)


# ---------------------------------------------------------------------------
# Self-test (spustit: python -m engine.ruleset)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    def _t(label, res, expect_tier, expect_success=None):
        ok = res.tier == expect_tier and (expect_success is None or res.is_success == expect_success)
        print(f"{'OK ' if ok else 'XX '}{label}: {res.tier.name} (margin {res.margin}, mit={res.mitigated})")
        assert ok, f"{label}: dostal {res.tier.name}, čekáno {expect_tier.name}"

    # Příklad oběd (DC 2, LOW)
    _t("oběd roll1", resolve_outcome(1, 0, 2, Stakes.LOW), OutcomeTier.CATASTROPHIC_FAILURE)
    _t("oběd roll10", resolve_outcome(10, 0, 2, Stakes.LOW), OutcomeTier.CLEAN_SUCCESS_BONUS, True)
    _t("oběd roll20", resolve_outcome(20, 0, 2, Stakes.LOW), OutcomeTier.EXCEPTIONAL_SUCCESS, True)

    # Démonický rituál (DC 40, EXTREME) — mág začátečník
    _t("rituál roll1", resolve_outcome(1, 0, 40, Stakes.EXTREME), OutcomeTier.CATASTROPHIC_FAILURE)
    _t("rituál roll19+6", resolve_outcome(19, 6, 40, Stakes.EXTREME), OutcomeTier.HARD_FAILURE, False)
    r = resolve_outcome(20, 6, 40, Stakes.EXTREME)
    _t("rituál roll20 (pod DC)", r, OutcomeTier.FAILURE, False)
    assert r.mitigated, "nat20 pod DC má být mitigated"

    # DC 12 škála (default medium) — musí přesně sednout na uživatelův příklad
    for total, exp in [
        (6, OutcomeTier.HARD_FAILURE), (7, OutcomeTier.SEVERE_FAILURE),
        (9, OutcomeTier.SEVERE_FAILURE), (10, OutcomeTier.FAILURE),
        (11, OutcomeTier.FAILURE), (12, OutcomeTier.SMALL_SUCCESS),
        (13, OutcomeTier.SMALL_SUCCESS), (14, OutcomeTier.CLEAN_SUCCESS),
        (18, OutcomeTier.CLEAN_SUCCESS), (19, OutcomeTier.CLEAN_SUCCESS_BONUS),
    ]:
        # roll v 2–19, modifiers tak aby total seděl; použijeme roll=min(total,19) bez mods kde lze
        roll = total if 2 <= total <= 19 else 10
        mods = total - roll
        _t(f"DC12 total={total}", resolve_outcome(roll, mods, 12, Stakes.MEDIUM), exp)

    # world_pressure modifikátor (jen sociální akce)
    for p, exp in [(0, 0), (2, 0), (3, -1), (4, -1), (5, -2), (6, -2), (7, -3), (9, -3), (10, -4), (15, -4)]:
        got = world_pressure_modifier(p)
        assert got == exp, f"pressure {p}: dostal {got}, čekáno {exp}"
    assert ADVENTURER_RULESET.action("INFLUENCE").affected_by_pressure
    assert ADVENTURER_RULESET.action("MANIPULATE").affected_by_pressure
    assert not ADVENTURER_RULESET.action("ATTACK").affected_by_pressure
    assert not ADVENTURER_RULESET.action("STEALTH").affected_by_pressure
    print("world_pressure + pressure flags OK")

    # difficulty tiery + smrt
    assert not DIFFICULTY_STORY.death_allowed and not DIFFICULTY_NORMAL.death_allowed
    assert DIFFICULTY_HARD.death_allowed and DIFFICULTY_INSANE.death_allowed
    assert resolve_hp_zero(True) == "game_over"
    assert resolve_hp_zero(False) == "knocked_out"
    assert set(DIFFICULTY_PROFILES) == {"story", "normal", "hard", "insane"}

    # dice parsing + combat
    import random
    assert DiceSpec.parse("1d8") == DiceSpec(1, 8)
    assert DiceSpec.parse("1d10+2") == DiceSpec(1, 10, 2)
    assert DiceSpec.parse("4") == DiceSpec(0, 0, 4)
    rng = random.Random(42)
    dmg = melee_damage(ADVENTURER_WEAPONS["sword"], finesse_bonus=2, margin=0, armor=1, rng=rng)
    assert 1 <= dmg <= 8 + 2 - 1, f"sword dmg mimo rozsah: {dmg}"
    # vysoký margin přidá bonus 1d4 → vyšší strop
    hi = max(melee_damage(ADVENTURER_WEAPONS["dagger"], 0, 7, 0, random.Random(s)) for s in range(50))
    assert hi > 6, f"bonus 1d4 se neprojevil: {hi}"
    print("combat / HP / dice OK")

    print("\nVšechny testy prošly.")
