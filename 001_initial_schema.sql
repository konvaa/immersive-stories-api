-- ============================================================
-- AI Immersive Stories — MVP-0 Initial Schema
-- Run this in Supabase SQL Editor
-- ============================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ============================================================
-- campaigns
-- ============================================================
CREATE TABLE campaigns (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id          UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    template         VARCHAR(50) NOT NULL DEFAULT 'adventurer',
    seed             BIGINT NOT NULL,
    engine_version   VARCHAR(20) NOT NULL DEFAULT '1.0.0',
    ruleset_version  VARCHAR(20) NOT NULL DEFAULT '1.0.0',
    player_state     JSONB NOT NULL DEFAULT '{}',
    world_pressure   INTEGER NOT NULL DEFAULT 2,
    tick             INTEGER NOT NULL DEFAULT 0,
    time_of_day      VARCHAR(20) NOT NULL DEFAULT 'morning',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_played_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- RLS
ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage their own campaigns"
    ON campaigns FOR ALL
    USING (auth.uid() = user_id);


-- ============================================================
-- npcs
-- ============================================================
CREATE TABLE npcs (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    campaign_id  UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    template_id  VARCHAR(100) NOT NULL,
    name         VARCHAR(200) NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    location_id  VARCHAR(100) NOT NULL DEFAULT 'crossroads_inn',
    trust        FLOAT NOT NULL DEFAULT 0,
    fear         FLOAT NOT NULL DEFAULT 0,
    suspicion    FLOAT NOT NULL DEFAULT 0,
    hp           FLOAT NOT NULL DEFAULT 100,
    state_json   JSONB NOT NULL DEFAULT '{"knowledge_flags": {}, "constraints": {}, "exposure_history": {}}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- RLS
ALTER TABLE npcs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage NPCs in their campaigns"
    ON npcs FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM campaigns
            WHERE campaigns.id = npcs.campaign_id
            AND campaigns.user_id = auth.uid()
        )
    );


-- ============================================================
-- locations
-- ============================================================
CREATE TABLE locations (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    campaign_id    UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    location_id    VARCHAR(100) NOT NULL,
    name           VARCHAR(200) NOT NULL,
    known_exits    TEXT[] NOT NULL DEFAULT '{}',
    flags          JSONB NOT NULL DEFAULT '{}',
    first_visited  INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(campaign_id, location_id)
);

-- RLS
ALTER TABLE locations ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage locations in their campaigns"
    ON locations FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM campaigns
            WHERE campaigns.id = locations.campaign_id
            AND campaigns.user_id = auth.uid()
        )
    );


-- ============================================================
-- events (append-only — NO UPDATE, NO DELETE)
-- ============================================================
CREATE TABLE events (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    campaign_id        UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    request_id         UUID NOT NULL,
    tick               INTEGER NOT NULL,
    engine_version     VARCHAR(20) NOT NULL,
    actor              VARCHAR(100) NOT NULL DEFAULT 'player',
    intent_type        VARCHAR(50) NOT NULL,
    action_type        VARCHAR(50) NOT NULL,
    raw_intent         TEXT NOT NULL,
    normalized_intent  VARCHAR(500) NOT NULL DEFAULT '',
    context_signature  VARCHAR(64) NOT NULL,
    rng_seed           INTEGER NOT NULL,
    roll               INTEGER NOT NULL,
    dc                 INTEGER NOT NULL,
    stakes             VARCHAR(20) NOT NULL,
    outcome            VARCHAR(50) NOT NULL,
    effects            JSONB NOT NULL DEFAULT '[]',
    causal_tags        TEXT[] NOT NULL DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(campaign_id, request_id)
);

-- RLS — append-only: SELECT and INSERT only, no UPDATE or DELETE
ALTER TABLE events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read events in their campaigns"
    ON events FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM campaigns
            WHERE campaigns.id = events.campaign_id
            AND campaigns.user_id = auth.uid()
        )
    );

CREATE POLICY "Users can insert events in their campaigns"
    ON events FOR INSERT
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM campaigns
            WHERE campaigns.id = events.campaign_id
            AND campaigns.user_id = auth.uid()
        )
    );

-- Explicitly deny UPDATE and DELETE via RLS (no policies = denied)
-- No UPDATE policy = UPDATE denied
-- No DELETE policy = DELETE denied


-- ============================================================
-- event_narratives (render layer — replaceable)
-- ============================================================
CREATE TABLE event_narratives (
    event_id           UUID PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    campaign_id        UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    model              VARCHAR(100) NOT NULL DEFAULT 'claude-sonnet-4-6',
    prompt_version     VARCHAR(20) NOT NULL DEFAULT '1.0.0',
    context_signature  VARCHAR(64) NOT NULL,
    narrative          TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- RLS
ALTER TABLE event_narratives ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage narratives in their campaigns"
    ON event_narratives FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM campaigns
            WHERE campaigns.id = event_narratives.campaign_id
            AND campaigns.user_id = auth.uid()
        )
    );


-- ============================================================
-- campaign_snapshots (cold boot cache only)
-- ============================================================
CREATE TABLE campaign_snapshots (
    campaign_id    UUID PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,
    snapshot_json  JSONB NOT NULL DEFAULT '{}',
    tick           INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- RLS
ALTER TABLE campaign_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage snapshots in their campaigns"
    ON campaign_snapshots FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM campaigns
            WHERE campaigns.id = campaign_snapshots.campaign_id
            AND campaigns.user_id = auth.uid()
        )
    );


-- ============================================================
-- Indexes for common queries
-- ============================================================
CREATE INDEX idx_campaigns_user_id ON campaigns(user_id);
CREATE INDEX idx_npcs_campaign_id ON npcs(campaign_id);
CREATE INDEX idx_locations_campaign_id ON locations(campaign_id);
CREATE INDEX idx_events_campaign_id ON events(campaign_id);
CREATE INDEX idx_events_request_id ON events(request_id);
CREATE INDEX idx_events_tick ON events(campaign_id, tick);
CREATE INDEX idx_event_narratives_campaign_id ON event_narratives(campaign_id);


-- ============================================================
-- Verification queries (run after migration to confirm)
-- ============================================================
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public'
-- ORDER BY table_name;
