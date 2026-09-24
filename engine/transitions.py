"""
engine/transitions.py — Správa přechodů mezi herními stavy a scénami.

Definuje a vyhodnocuje podmínky pro přechod kampaně z jednoho stavu do
druhého (vstup do nové lokace, spuštění události, konec kampaně…).
Pracuje s modelem stavového automatu popsaným v GDD.

Dle GDD: přechody řídí tok příběhu a zajišťují konzistenci světa.
"""
