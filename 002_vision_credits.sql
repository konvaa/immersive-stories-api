-- ============================================================================
-- 002_vision_credits.sql — Vision Credits ledger + generation requests
-- Dle GDD Addendum G (23.18) a H (23.26, 23.27).
-- Spustit v Supabase SQL editoru.
-- ============================================================================

-- Každý pohyb kreditů. Append-only — žádný UPDATE/DELETE.
CREATE TABLE vision_credit_ledger (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL,
    delta          integer NOT NULL,            -- kladné = připsání, záporné = útrata
    balance_after  integer NOT NULL,            -- audit pole
    bucket_type    varchar NOT NULL DEFAULT 'free',   -- 'free' | 'promo' | 'paid'
    origin         varchar NOT NULL,            -- 'onboarding' | 'weekly' | 'achievement'
                                                -- | 'purchase' | 'spend' | 'refund' | 'admin'
    reference_id   uuid,                        -- generation_request_id | purchase_id
    expires_at     timestamptz,                 -- NULL = bez expirace (paid)
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_vcl_user ON vision_credit_ledger (user_id, created_at DESC);

-- Append-only enforcement
ALTER TABLE vision_credit_ledger ENABLE ROW LEVEL SECURITY;
CREATE POLICY vcl_no_update ON vision_credit_ledger FOR UPDATE USING (false);
CREATE POLICY vcl_no_delete ON vision_credit_ledger FOR DELETE USING (false);

-- ----------------------------------------------------------------------------
-- Jeden business požadavek na vizuál. UNIQUE constraint = ochrana proti
-- double-charge při timeoutu, retry nebo double-tapu (GDD 23.18).
-- ----------------------------------------------------------------------------
CREATE TABLE vision_generation_requests (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL,
    campaign_id     uuid NOT NULL REFERENCES campaigns(id),
    event_id        uuid NOT NULL REFERENCES events(id),
    render_type     varchar NOT NULL DEFAULT 'scene',  -- 'scene' | 'epic'
    credit_cost     integer NOT NULL,
    status          varchar NOT NULL DEFAULT 'pending', -- 'pending' | 'success'
                                                        -- | 'failed' | 'refunded'
    image_url       text,
    failure_reason  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    UNIQUE (user_id, campaign_id, event_id, render_type)
);

CREATE INDEX idx_vgr_pending ON vision_generation_requests (user_id, status)
    WHERE status = 'pending';
