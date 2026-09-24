"""
llm/image_prompt_builder.py — Sestaví prompt pro DALL-E 3.

Pravidla:
  - Nikdy nepoužívat raw player text přímo.
  - Nikdy neobsahovat jména postav jako text v obrázku.
  - Max 150 slov.
  - Styl: dark fantasy RPG illustration, dramatic lighting, painterly style.
"""

from __future__ import annotations

from models.state import NPCSnapshot, LocationSnapshot


_STYLE_PREFIX = (
    "Dark fantasy RPG illustration, dramatic lighting, painterly style, "
    "detailed environment, no text, no labels, no UI elements. "
)

# Mapování action_type → vizuální slovník
_ACTION_VISUAL: dict[str, str] = {
    "SPEECH":    "A tense conversation unfolds",
    "EXAMINE":   "A figure carefully examines the surroundings",
    "MOVE":      "A figure moves through the scene",
    "ATTACK":    "A sudden violent confrontation",
    "STEALTH":   "A shadowed figure moves unseen",
    "SOCIAL":    "Characters interact in close proximity",
    "UNKNOWN":   "A moment frozen in time",
}


def build_image_prompt(
    event: dict,
    campaign: dict,
    npcs: list[NPCSnapshot],
    location: LocationSnapshot | None,
) -> str:
    """
    Sestaví prompt pro DALL-E 3 z herního stavu.

    Args:
        event:    Row z tabulky events (raw_intent, action_type, outcome).
        campaign: Row z tabulky campaigns (template).
        npcs:     NPCSnapshot objekty přítomné v aktuální lokaci.
        location: LocationSnapshot aktuální lokace (nebo None).

    Returns:
        Prompt string ≤ 150 slov.
    """
    parts: list[str] = [_STYLE_PREFIX]

    # Lokace
    if location:
        parts.append(f"Setting: {location.name}.")
    else:
        parts.append("Setting: a dimly lit tavern interior.")

    # NPC přítomní v lokaci
    present_npcs = [n for n in npcs if n.location_id == (location.location_id if location else "")]
    if present_npcs:
        npc_descs = []
        for npc in present_npcs[:3]:   # max 3 NPC aby prompt nepřekročil limit
            npc_descs.append(npc.name)
        parts.append(f"Characters present: {', '.join(npc_descs)}.")

    # Akce → vizuální popis (nikdy raw player text)
    action_type = (event.get("action_type") or "UNKNOWN").upper()
    visual_action = _ACTION_VISUAL.get(action_type, _ACTION_VISUAL["UNKNOWN"])
    outcome = (event.get("outcome") or "CLEAN_SUCCESS").upper()
    if outcome in ("EXCEPTIONAL_SUCCESS", "CLEAN_SUCCESS_BONUS", "CLEAN_SUCCESS",
                   "CRITICAL_SUCCESS", "SUCCESS"):           # + legacy
        mood = "triumphant tension"
    elif outcome in ("SMALL_SUCCESS", "PARTIAL_SUCCESS"):    # + legacy
        mood = "uncertain resolve"
    elif outcome in ("CATASTROPHIC_FAILURE", "HARD_FAILURE"):
        mood = "catastrophic aftermath, dread"
    else:
        mood = "grim consequence"
    parts.append(f"Scene: {visual_action}. Mood: {mood}.")

    # Template → atmosféra
    template = (campaign.get("template") or "adventurer").lower()
    if "adventurer" in template:
        parts.append("Atmosphere: medieval frontier, worn leather and iron, candlelight.")

    prompt = " ".join(parts)

    # Tvrdý limit 150 slov
    words = prompt.split()
    if len(words) > 150:
        prompt = " ".join(words[:150])

    return prompt
