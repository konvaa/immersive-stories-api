"""
engine/classifier.py — Deterministický rule-based klasifikátor hráčských akcí.

Vzory dle GDD sekce 20.1 + reconciliation 2026-06-13 (MANIPULATE, STEALTH):
  OBSERVE:     look, watch, observe, scan, glance
  INVESTIGATE: search, examine, investigate, explore, ask about, inquire
  MOVE:        go, walk, move, head, leave, enter
  INFLUENCE:   talk, speak, say, tell, convince, persuade, threaten, smile, nod
  MANIPULATE:  jednoznačná slovesa (forge, falsify, tamper) + slovesné fráze
               s členem/zájmenem (plant/fake/doctor/alter/rewrite the X),
               cover tracks, hide/conceal the <objekt>. Holá slova fake/plant/
               alter NEmatchují (audit V3: 'look at the plant' ≠ manipulace).
  STEALTH:     sneak, hide, creep, skulk, tiptoe, slip past, blend in, stealth
  ATTACK:      attack, strike, hit, fight
  INTERACT:    use, take, open, grab, pick up
  WAIT:        wait, rest, sleep, do nothing
  UNKNOWN:     cokoliv jiného

Pořadí v _PATTERNS určuje prioritu (první shoda vyhraje).
"""

from __future__ import annotations

import re
from enum import Enum


class ActionType(str, Enum):
    OBSERVE     = "OBSERVE"
    INVESTIGATE = "INVESTIGATE"
    MOVE        = "MOVE"
    INFLUENCE   = "INFLUENCE"
    MANIPULATE  = "MANIPULATE"   # změna známých faktů / skrytého stavu (ne osoby)
    STEALTH     = "STEALTH"      # skryté / nenápadné jednání
    ATTACK      = "ATTACK"
    INTERACT    = "INTERACT"
    WAIT        = "WAIT"
    UNKNOWN     = "UNKNOWN"


# Ordered list: (ActionType, compiled regex).
# Patterns are checked against lowercased intent; first match wins.
_PATTERNS: list[tuple[ActionType, re.Pattern[str]]] = [
    (ActionType.ATTACK,      re.compile(r'\b(attack|strike|hit|fight)\b')),
    # MANIPULATE před STEALTH i INVESTIGATE: 'hide the evidence' / 'cover the tracks'
    # je manipulace faktů, ne skrývání sebe. Ale (audit V3): víceznačná slova
    # (fake/plant/doctor/alter/rewrite) matchují JEN jako sloveso s členem či
    # zájmenem za sebou — 'plant the dagger' ano, 'look at the plant' ne.
    (ActionType.MANIPULATE,  re.compile(
        r'\b(forge|falsify|tamper'
        r'|(?:plant|fake|doctor|alter|rewrite)\s+(?:the|a|an|his|her|their|my|its)\b'
        r'|cover\s+(?:the\s+|my\s+|our\s+)?tracks'
        r'|hide\s+the\s+\w+|conceal\s+the\s+\w+)\b')),
    (ActionType.STEALTH,     re.compile(
        r'\b(sneak|hide|creep|skulk|tiptoe|slip\s+past|blend\s+in|stealth(?:ily)?)\b')),
    (ActionType.INVESTIGATE, re.compile(r'\b(search|examine|investigate|explore|ask\s+about|inquire)\b')),
    (ActionType.OBSERVE,     re.compile(r'\b(look|watch|observe|scan|glance)\b')),
    (ActionType.MOVE,        re.compile(r'\b(go|walk|move|head|leave|enter)\b')),
    (ActionType.INFLUENCE,   re.compile(r'\b(talk|speak|say|tell|convince|persuade|threaten|smile|nod)\b')),
    (ActionType.INTERACT,    re.compile(r'\b(use|take|open|grab|pick\s+up)\b')),
    (ActionType.WAIT,        re.compile(r'\b(wait|rest|sleep|do\s+nothing)\b')),
]


def classify(intent: str) -> ActionType:
    """
    Klasifikuje volný text hráčova záměru na ActionType.
    Deterministické — žádný náhodný prvek, žádné LLM volání.
    """
    text = intent.lower()
    for action_type, pattern in _PATTERNS:
        if pattern.search(text):
            return action_type
    return ActionType.UNKNOWN
