-- Adds 'skipped' now that the handler layer exists to decide it — see
-- 001_init.sql's comment on urls.status, and DESIGN.md's "unmatched
-- content type" decision. A plain ALTER, not a native-ENUM change: exactly
-- the case the TEXT + CHECK choice was made for.

ALTER TABLE urls DROP CONSTRAINT urls_status_check;
ALTER TABLE urls ADD CONSTRAINT urls_status_check
    CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'skipped'));

-- Fences a terminal write to the claim that actually made it. claim_batch
-- alone doesn't stop two workers from both holding what they each think is
-- the current claim on one row: a worker whose lease expired gets its row
-- reset to 'pending' by recover_expired_leases and reclaimed by someone
-- else, but the first worker doesn't know that — it's still mid-fetch,
-- and without something to check against, its eventual mark_done would
-- overwrite whatever the second worker already wrote. gen_random_uuid()
-- is core Postgres since 13, no extension needed.
ALTER TABLE urls ADD COLUMN IF NOT EXISTS lease_token UUID;
