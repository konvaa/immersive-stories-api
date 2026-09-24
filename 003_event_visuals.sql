-- ============================================================================
-- 003_event_visuals.sql — Dokumentace tabulky event_visuals (GDD 23.5, 23.29)
-- Tabulka byla původně vytvořena ručně v Supabase; tento soubor ji zachycuje
-- pro reprodukovatelnost schématu. IF NOT EXISTS → bezpečné spustit znovu.
-- ============================================================================

CREATE TABLE IF NOT EXISTS event_visuals (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id               uuid NOT NULL REFERENCES events(id),
    campaign_id            uuid NOT NULL REFERENCES campaigns(id),
    generation_request_id  uuid,
    original_image_url     text NOT NULL,
    prompt                 text,
    provider               varchar,          -- 'gpt-image-1' | 'dalle3' | ...
    test_mode              boolean DEFAULT true,
    provider_call_count    integer DEFAULT 1,
    created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_event_visuals_event
    ON event_visuals (event_id);
CREATE INDEX IF NOT EXISTS idx_event_visuals_campaign
    ON event_visuals (campaign_id, created_at DESC);

-- Pozn.: Supabase Storage bucket 'event-visuals' (public) je vytvořen ručně
-- v Dashboardu — Storage buckety nejsou součástí SQL migrace.
