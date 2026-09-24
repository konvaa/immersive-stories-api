# Deploy checklist

## Povinné pořadí kroků (audit K1)

Nový kód (resolver zásah, adventurer-0.1.0) čte `events.mitigated`, `events.stakes`
v idempotency lookupu a `campaigns.difficulty`. **Bez migrace 004 spadne každý
request na /action** (UndefinedColumnError) — nejde o degradaci, ale o výpadek.
Proto vždy v tomto pořadí:

1. **Supabase SQL editor: spustit `004_adventurer_ruleset.sql`.**
   Přidá `campaigns.difficulty`, `events.mitigated`, přepne ruleset_version
   na `adventurer-0.1.0`, doplní player skilly a HP škálu 20.
2. **(dev) Volitelně: `scripts/dev_reset_campaigns.sql`** — čistý wipe testovacích
   kampaní místo migrace dat (viz níže). Nemaže `auth.users` ani
   `vision_credit_ledger`.
3. **Railway env: `RULESET_VERSION=adventurer-0.1.0`.**
4. **Push na main → Railway deploy.** Pokud je Railway nastavené na auto-deploy
   z main, nepushuj na main před aplikací migrace 004.
5. **Smoke test:** `GET /health` → ok; založit kampaň, poslat akci.
6. **Živé akceptační testy:** lokálně `uvicorn main:app` + `pytest tests/ -v`
   (vyžaduje TEST_JWT_TOKEN v .env).

## Dev reset testovacích dat

Projekt není vydaný — kampaně v DB jsou dev data. Po změně engine kontraktu
(HP škála, ruleset_version, skilly) je čistší testovací kampaně smazat než
migrovat: `scripts/dev_reset_campaigns.sql`.

Skript maže POUZE herní stav: event_visuals, vision_generation_requests,
event_narratives, events, campaign_snapshots, npcs, locations, campaigns.
Záměrně NEmaže: `auth.users`, `vision_credit_ledger` (append-only audit
kreditů vázaný na uživatele).

## Poznámky

- Replay kontrakt: `events.rng_seed` je uložený — změna ruleset_version
  nemění interpretaci historických eventů (replay čte uložený seed a efekty).
- Idempotency je scoped na `(campaign_id, request_id)` (audit K2) — klient smí
  recyklovat request_id napříč kampaněmi, ne uvnitř jedné kampaně.
