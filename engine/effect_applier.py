"""
engine/effect_applier.py — Aplikátor efektů na herní stav (in-memory).

Přebírá FrozenWorldView + seznam Effect, vrací AppliedState — strukturu
s mutovanými snapshoty připravenými k zápisu do DB přes db.writer.

Dle GDD: effect_applier je jediný modul oprávněný „zapisovat" do stavu,
ale pracuje pouze v paměti; persistenci zajišťuje db.writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from models.state import Effect, EffectType, FrozenWorldView


@dataclass
class NPCDelta:
    """Změny jednoho NPC připravené pro UPDATE do DB."""
    npc_id: str
    trust: int
    fear: int
    suspicion: int
    hp: int
    state_json: dict          # disposition, flags, knowledge


@dataclass
class AppliedState:
    """Výsledek aplikace efektů — vše připravené pro db.writer."""
    player_state: dict        # aktualizovaný campaigns.player_state (JSONB)
    npc_deltas: list[NPCDelta] = field(default_factory=list)
    location_flag_updates: list[dict] = field(default_factory=list)  # {row_id, flags}
    time_of_day: str = "morning"
    world_pressure: int = 0
    tick: int = 0             # nový tick (world.tick + 1)


def apply_effects(world: FrozenWorldView, effects: list[Effect]) -> AppliedState:
    """
    Aplikuje efekty na kopie stavů z world a vrátí AppliedState.

    Priorita: efekty jsou aplikovány v pořadí — pozdější SET přepíše DELTA.
    """
    # --- mutable kopie player state ---
    player = world.player.model_dump()

    # --- mutable kopie NPC stavů (keyed by npc_id) ---
    npc_map: dict[str, dict] = {
        n.npc_id: {
            "npc_id":    n.npc_id,
            "trust":     n.trust,
            "fear":      n.fear,
            "suspicion": n.suspicion,
            "hp":        n.hp,
            "state_json": {
                "disposition": n.disposition,
                "flags":       list(n.flags),
                "knowledge":   list(n.knowledge),
            },
        }
        for n in world.npcs
    }

    # --- mutable kopie location flags (keyed by location_id) ---
    loc_map: dict[str, dict] = {
        l.location_id: {"row_id": l.row_id, "flags": list(l.flags)}
        for l in world.locations
    }

    time_of_day = world.time_of_day
    world_pressure = world.world_pressure

    for fx in effects:
        tid = fx.target_id

        # ---- Hráč ----
        if tid == player.get("player_id") or tid == "player":
            if fx.type == EffectType.ADD_KNOWLEDGE:
                know = player.setdefault("knowledge", [])
                if fx.knowledge and fx.knowledge not in know:
                    know.append(fx.knowledge)
            elif fx.type == EffectType.ADD_FLAG:
                flags = player.setdefault("flags", [])
                if fx.flag and fx.flag not in flags:
                    flags.append(fx.flag)
            elif fx.type == EffectType.REMOVE_FLAG:
                player["flags"] = [f for f in player.get("flags", []) if f != fx.flag]
            elif fx.type == EffectType.DELTA and fx.attribute:
                player[fx.attribute] = player.get(fx.attribute, 0) + (fx.value or 0)
            elif fx.type == EffectType.SET and fx.attribute:
                player[fx.attribute] = fx.value
            elif fx.type == EffectType.MOVE_ENTITY and fx.destination:
                player["location_id"] = fx.destination

        # ---- Svět (time_of_day, world_pressure) ----
        elif tid == "world":
            if fx.type == EffectType.SET and fx.attribute == "time_of_day":
                time_of_day = str(fx.value)
            elif fx.type == EffectType.SET and fx.attribute == "world_pressure":
                world_pressure = int(fx.value or 0)
            elif fx.type == EffectType.DELTA and fx.attribute == "world_pressure":
                world_pressure += int(fx.value or 0)

        # ---- NPC ----
        elif tid in npc_map:
            npc = npc_map[tid]
            if fx.type == EffectType.DELTA and fx.attribute in ("trust", "fear", "suspicion", "hp"):
                npc[fx.attribute] = max(0, min(100, npc[fx.attribute] + (fx.value or 0)))
            elif fx.type == EffectType.SET and fx.attribute:
                npc[fx.attribute] = fx.value
            elif fx.type == EffectType.ADD_FLAG:
                sj = npc["state_json"]
                if fx.flag and fx.flag not in sj["flags"]:
                    sj["flags"].append(fx.flag)
            elif fx.type == EffectType.REMOVE_FLAG:
                npc["state_json"]["flags"] = [
                    f for f in npc["state_json"]["flags"] if f != fx.flag
                ]
            elif fx.type == EffectType.ADD_KNOWLEDGE:
                sj = npc["state_json"]
                if fx.knowledge and fx.knowledge not in sj["knowledge"]:
                    sj["knowledge"].append(fx.knowledge)

        # ---- Lokace ----
        elif tid in loc_map:
            loc = loc_map[tid]
            if fx.type == EffectType.ADD_FLAG:
                if fx.flag and fx.flag not in loc["flags"]:
                    loc["flags"].append(fx.flag)
            elif fx.type == EffectType.REMOVE_FLAG:
                loc["flags"] = [f for f in loc["flags"] if f != fx.flag]

    # Clamp HP hráče
    player["hp"] = max(0, min(player.get("max_hp", 100), player.get("hp", 100)))

    npc_deltas = [
        NPCDelta(
            npc_id=v["npc_id"],
            trust=v["trust"],
            fear=v["fear"],
            suspicion=v["suspicion"],
            hp=v["hp"],
            state_json=v["state_json"],
        )
        for v in npc_map.values()
    ]

    location_flag_updates = list(loc_map.values())

    return AppliedState(
        player_state=player,
        npc_deltas=npc_deltas,
        location_flag_updates=location_flag_updates,
        time_of_day=time_of_day,
        world_pressure=world_pressure,
        tick=world.tick + 1,
    )
