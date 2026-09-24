-- ============================================================
-- scripts/dev_reset_campaigns.sql — DEV wipe herního stavu
-- Run in Supabase SQL Editor. POUZE pro dev/test prostředí!
--
-- Maže veškerý herní stav (kampaně a vše na ně navázané).
-- NEmaže: auth.users, vision_credit_ledger (append-only audit).
-- Pořadí respektuje FK bez ON DELETE CASCADE
-- (event_visuals, vision_generation_requests).
-- ============================================================

BEGIN;

DELETE FROM event_visuals;
DELETE FROM vision_generation_requests;
DELETE FROM event_narratives;
DELETE FROM events;
DELETE FROM campaign_snapshots;
DELETE FROM npcs;
DELETE FROM locations;
DELETE FROM campaigns;

COMMIT;

-- Verifikace:
-- SELECT (SELECT count(*) FROM campaigns) AS campaigns,
--        (SELECT count(*) FROM events)    AS events,
--        (SELECT count(*) FROM npcs)      AS npcs;
