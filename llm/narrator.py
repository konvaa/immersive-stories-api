"""
llm/narrator.py — Vypravěč příběhu poháněný Anthropic API (Claude).

Přebírá prompt sestavený přes context_builder a volá Anthropic Messages API.
Při selhání (timeout, API chyba, rate-limit) vrátí None — caller rozhodne
o fallback chování dle GDD 20.1.

Dle GDD: narrator je „hlas GM" — jeho výstup je to, co hráč čte/slyší.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_MODEL  = "claude-haiku-4-5-20251001"
_MAX_TOKENS     = 400
_SYSTEM_MESSAGE = """\
You are the narrator of a dark fantasy text RPG. You receive the engine resolution result and world context. Your role is to narrate what happened — nothing more.
Narration profile: compact.
Write for mobile reading. Target around 70 words. Hard maximum 110 words.
Use short paragraphs. Maximum 3 sentences per paragraph. Separate distinct beats with line breaks: action / reaction / observation / hint.
Do not write walls of text.
If the player action is vague, narrate only the vague action. Do not invent specific questions, motives, targets, facts, or discoveries unless present in EngineResult or verified world state.
Every specific fact in the narration must come from: EngineResult, NarrativeContext, current player input, or verified world state. If a fact is not from these sources, render it as uncertainty, suspicion, or rumor — never as confirmed fact.
If useful, end with a diegetic hint suggesting 1-3 possible focused follow-up directions.
Always respond in Czech regardless of the language of player input.
Example — vague action: Input: i talk to innkeeper WRONG: When you asked him about things that interest you, he revealed some useful information... CORRECT: Přistoupíš k hospodskému a navážeš opatrný rozhovor. Měří si tě, ale zatím neřekne nic konkrétního.
Můžeš se ho zeptat na: krev u schodů / zadní komoru / šerifa / kdo byl v hospodě před tebou.\
"""


def generate_narrative(prompt: str, model: str | None = None) -> str | None:
    """
    Zavolá Anthropic API a vrátí vygenerovaný narativ.

    Args:
        prompt: Hotový user prompt z context_builder.build_narrative_context().
        model:  Přepíše výchozí model (claude-haiku-4-5-20251001).

    Returns:
        Narativní text jako str, nebo None pokud API selže.
    """
    try:
        import anthropic  # lazy import — omezíme startup čas

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY není nastaven — narativ přeskočen")
            return None

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model or _DEFAULT_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_MESSAGE,
            messages=[{"role": "user", "content": prompt}],
        )
        text: str = response.content[0].text.strip()
        return text or None

    except Exception as exc:  # noqa: BLE001
        logger.error("Anthropic API selhal: %s", exc)
        return None


def used_model() -> str:
    """Vrátí název modelu použitého pro generování narativu."""
    return _DEFAULT_MODEL
