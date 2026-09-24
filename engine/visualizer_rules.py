"""
engine/visualizer_rules.py — Eligibility pravidla pro Scene Visualizer (GDD 23.3, 23.16).

Určuje, zda je event "visualizer-eligible" — tedy zda /action response
nabídne hráči vygenerování vizuálu (visualizer_available: true).

Dle GDD: pravidla jsou definována v ruleset configu, NE hardcoded v enginu.
MVP: jeden default config (Adventurer template). Phase 2: per-template
configy + cooldowny + suppression pravidla (Addendum G 23.21).
"""

from __future__ import annotations

from typing import Any

# Default trigger config — Adventurer template (GDD 23.16).
# Drow template dostane vlastní config s jinými kritérii.
VISUALIZER_TRIGGERS: dict[str, set[str]] = {
    # Akce, které jsou vizuálně významné samy o sobě.
    # INVESTIGATE/INTERACT jsou objevné/interaktivní momenty hodné vizuálu.
    "action_types": {"ATTACK", "INVESTIGATE", "INTERACT"},
    # Outcome kategorie = významný moment.
    # Čisté úspěchy přidány záměrně: scarcity zajišťuje cooldown (VISION_OFFER_COOLDOWN_TICKS),
    # ne úzká eligibilita — nabídka se tak objeví na nejbližším úspěšném momentu
    # zhruba jednou za cooldown period, místo aby se v zaběhlé kampani neukázala vůbec.
    # 8-tier systém (engine/ruleset.py) + legacy hodnoty pro eventy před migrací 004.
    "outcomes": {
        "EXCEPTIONAL_SUCCESS", "CLEAN_SUCCESS_BONUS", "CLEAN_SUCCESS",
        "CRITICAL_SUCCESS", "SUCCESS",   # legacy (staré eventy v idempotentní cache)
    },
    # Efekty znamenající novou scénu / objev
    "effect_types": {"MOVE_ENTITY", "DISCOVER_EXIT"},
}


def _effect_type_str(fx: Any) -> str:
    """Vrátí typ efektu jako string — funguje pro Effect objekt i dict."""
    if isinstance(fx, dict):
        raw = fx.get("type", "")
    else:
        raw = getattr(fx, "type", "")
    return getattr(raw, "value", raw) or ""


def is_visualizer_eligible(
    action_type: str,
    outcome: str,
    effects: list[Any],
    triggers: dict[str, set[str]] | None = None,
) -> bool:
    """
    True pokud event splňuje visualizer trigger pravidla.

    Args:
        action_type: engine action type (např. 'ATTACK')
        outcome:     outcome kategorie (např. 'CRITICAL_SUCCESS')
        effects:     list Effect objektů nebo dictů (z idempotentní cache)
        triggers:    přepis default configu (per-template ruleset)
    """
    cfg = triggers or VISUALIZER_TRIGGERS
    if action_type in cfg["action_types"]:
        return True
    if outcome in cfg["outcomes"]:
        return True
    return any(_effect_type_str(fx) in cfg["effect_types"] for fx in effects)
