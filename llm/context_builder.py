"""
llm/context_builder.py — Sestavovač kontextu pro LLM narátor.

Transformuje FrozenWorldView + výsledek resoluce do strukturovaného promptu
vhodného pro Anthropic API. Prioritizuje relevantní informace: aktuální lokace,
přítomné NPC, stav hráče, výsledek hodu.

Dle GDD: kvalita kontextu přímo určuje kvalitu narativu.
"""

from __future__ import annotations

from models.state import Effect, EffectType, FrozenWorldView


def build_narrative_context(
    world: FrozenWorldView,
    action_type: str,
    outcome: str,
    roll: int,
    dc: int,
    intent: str,
    effects: list[Effect],
    total: int | None = None,
    stakes: str | None = None,
    mitigated: bool = False,
) -> str:
    """
    Sestaví system + user prompt pro Claude.

    Vrací hotový string prompt ready pro messages API.
    """
    player = world.player
    location_id = player.location_id

    # Aktuální lokace
    current_loc = next(
        (l for l in world.locations if l.location_id == location_id), None
    )
    loc_name = current_loc.name if current_loc else location_id
    loc_exits = ", ".join(current_loc.known_exits) if current_loc else "žádné"

    # NPC v lokaci
    local_npcs = [n for n in world.npcs if n.location_id == location_id]
    npc_lines = "\n".join(
        f"  - {n.name} (důvěra:{n.trust}, podezření:{n.suspicion}, HP:{n.hp})"
        for n in local_npcs
    ) or "  (nikdo)"

    # Shrnutí efektů
    effect_lines = []
    for fx in effects:
        if fx.type == EffectType.DELTA:
            sign = "+" if (fx.value or 0) >= 0 else ""
            effect_lines.append(f"  {fx.target_id}.{fx.attribute} {sign}{fx.value}")
        elif fx.type == EffectType.ADD_KNOWLEDGE:
            effect_lines.append(f"  hráč získal znalost: {fx.knowledge}")
        elif fx.type == EffectType.MOVE_ENTITY:
            effect_lines.append(f"  hráč se přesunul do: {fx.destination}")
        elif fx.type == EffectType.SET:
            effect_lines.append(f"  {fx.target_id}.{fx.attribute} = {fx.value}")
        elif fx.type in (EffectType.ADD_FLAG, EffectType.REMOVE_FLAG):
            action = "přidán" if fx.type == EffectType.ADD_FLAG else "odebrán"
            effect_lines.append(f"  příznak {fx.flag} {action} pro {fx.target_id}")
    effects_summary = "\n".join(effect_lines) or "  (žádné)"

    shown_total = total if total is not None else roll
    stakes_part = f"  |  Stakes: {stakes}" if stakes else ""
    mitigated_part = (
        "\nPozn.: hráč hodil natural 20, ale nestačilo — selhání je MÍRNÉ, bez"
        "\nvážného následku (těsně to nevyšlo, hráč vyvázl bez úhony)."
        if mitigated else ""
    )

    prompt = f"""Jsi vypravěč (Game Master) interaktivní fantasy hry. Piš stručně, atmosfericky, ve 2–4 větách.
Neodhaluj herní mechaniky ani čísla. Piš ve druhé osobě singuláru (ty).

=== STAV SVĚTA ===
Lokace:    {loc_name}  |  Čas: {world.time_of_day}
Východy:   {loc_exits}
HP hráče:  {player.hp}/{player.max_hp}  |  Zlato: {player.gold}

NPC v lokaci:
{npc_lines}

=== AKCE HRÁČE ===
Záměr:     {intent}
Typ akce:  {action_type}
Výsledek:  {outcome}  (hod {shown_total} vs DC {dc}){stakes_part}{mitigated_part}

Mechanické změny (NESMÍ být zmíněny v textu):
{effects_summary}

=== ÚKOL ===
Napiš narativní odstavec popisující, co se stalo jako přímý důsledek akce hráče.
Výsledek {outcome} musí být patrný z tónu a událostí. Použij druhý pád singuláru."""

    return prompt
