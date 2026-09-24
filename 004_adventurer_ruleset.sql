-- ============================================================
-- 004 — Adventurer ruleset 0.1.0 (reconciliation sekce E)
-- Run this in Supabase SQL Editor
--
-- !!! POVINNÉ PŘED DEPLOYEM kódu s resolver zásahem (audit K1):
-- kód selectuje events.mitigated/stakes v idempotency checku —
-- bez této migrace spadne KAŽDÝ request na /action.
-- Pořadí kroků: docs/DEPLOY.md. Dev data lze místo migrace vyčistit
-- přes scripts/dev_reset_campaigns.sql.
--
-- 1. campaigns.difficulty — zvolený difficulty tier (DIFFICULTY_PROFILES)
-- 2. events.mitigated    — natural 20 změkčilo neúspěch (propsáno do API)
-- 3. ruleset_version     — legacy '1.0.0' → 'adventurer-0.1.0'
--    (rng_seed derivace: nové eventy použijí novou verzi; replay čte
--     ULOŽENÝ events.rng_seed, historie zůstává deterministická)
-- 4. player_state        — core skilly (C1) + přechod na HP škálu 20
-- 5. npcs.hp             — přeškálování 100→20 (Innkeeper 80→16, Sheriff 100→20),
--    aby melee_damage (1d4–1d10+2) dával smysluplné souboje
-- ============================================================

-- 1. Difficulty tier kampaně
ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS difficulty VARCHAR(20) NOT NULL DEFAULT 'normal';

-- 2. Mitigated flag eventu
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS mitigated BOOLEAN NOT NULL DEFAULT FALSE;

-- 3. Ruleset verze
UPDATE campaigns
SET ruleset_version = 'adventurer-0.1.0'
WHERE ruleset_version = '1.0.0';

-- 4. Player state: skilly + HP škála 20
--    (skills jen pokud chybí; hp/max_hp z legacy 100-škály na 20)
UPDATE campaigns
SET player_state = player_state
    || jsonb_build_object(
         'skills', COALESCE(
             player_state->'skills',
             '{"awareness": 10, "resolve": 10, "presence": 10, "finesse": 10}'::jsonb
         ),
         'max_hp', 20,
         'hp', LEAST(
             GREATEST(COALESCE((player_state->>'hp')::int, 20), 0),
             20
         )
       )
WHERE COALESCE((player_state->>'max_hp')::int, 100) > 20
   OR player_state->'skills' IS NULL;

-- 5. NPC HP přeškálování (legacy 100-škála → 20-škála)
UPDATE npcs
SET hp = ROUND(hp * 20.0 / 100.0)
WHERE hp > 20;

-- ============================================================
-- Verifikace:
-- SELECT id, ruleset_version, difficulty,
--        player_state->>'hp' AS hp, player_state->'skills' AS skills
-- FROM campaigns;
-- SELECT name, hp FROM npcs;
-- ============================================================
