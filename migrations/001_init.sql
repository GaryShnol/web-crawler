-- Frontier state, content dedup, and the discovery graph. See CLAUDE.md:
-- correctness lives here, not in process memory.

-- One row per unique payload, keyed by its hash — "identical bytes from two
-- urls are stored once". content_type/byte_size describe the stored blob;
-- urls.content_type/content_length below describe what was *observed*,
-- which still applies to a skipped url that was never downloaded at all.
CREATE TABLE contents (
    content_hash  TEXT PRIMARY KEY,
    content_type  TEXT NOT NULL,
    byte_size     BIGINT NOT NULL,
    storage_path  TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE urls (
    id              BIGSERIAL PRIMARY KEY,

    -- normalized_url is the identity: the conflict target, and what
    -- actually gets fetched. raw_url is only ever set once, at insert, from
    -- whichever spelling first produced the row — a debugging hint, not a
    -- history of every variant seen.
    normalized_url  TEXT NOT NULL UNIQUE,
    raw_url         TEXT NOT NULL,

    depth           INT NOT NULL DEFAULT 0,

    -- TEXT + CHECK, not a native ENUM: adding a status later is a plain
    -- migration, not an ALTER TYPE that can't run in the same transaction
    -- as its first use — 'skipped' (DESIGN.md: unmatched content type)
    -- isn't here yet because nothing writes it until the handler layer
    -- exists to decide it; add it in the migration that adds that handler.
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'in_progress', 'done', 'failed')),

    -- Bumped only by claim_batch's own claiming statement — never by
    -- whatever later sends the row back to 'pending' (retry, release).
    attempts        INT NOT NULL DEFAULT 0,

    -- Set on claim, cleared on every terminal write. A lease that's simply
    -- aged past lease_until, with no worker coming back for it, is what a
    -- crashed worker leaves behind — see store/frontier.py's
    -- recover_expired_leases.
    lease_until     TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    content_type    TEXT,
    content_length  BIGINT,
    content_hash    TEXT REFERENCES contents (content_hash),
    etag            TEXT,

    error_kind      TEXT,
    error_message   TEXT,

    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- claim_batch and mark_* only ever scan or touch one status at a time; a
-- partial index keeps that scan small as 'done'/'failed' rows pile up,
-- instead of growing with the whole table.
CREATE INDEX urls_pending_idx ON urls (next_attempt_at) WHERE status = 'pending';
CREATE INDEX urls_in_progress_idx ON urls (lease_until) WHERE status = 'in_progress';

-- Per-type extraction, keyed by content_hash like `contents` — two urls
-- sharing bytes share the extraction too, so it's never redone or
-- duplicated for a second url that happens to hash the same.
CREATE TABLE content_metadata (
    content_hash TEXT PRIMARY KEY REFERENCES contents (content_hash),
    kind         TEXT NOT NULL,
    payload      JSONB NOT NULL
);

-- The discovery graph: which page linked to which, with what anchor text.
-- PRIMARY KEY (src_id, dst_id) means the edge is deduplicated by endpoints
-- alone: if the same page links to the same target twice with different
-- anchor text, ON CONFLICT DO NOTHING in enqueue_many keeps only the first
-- anchor text seen and silently drops the second. Acknowledged, not fixed —
-- the alternative is a row per (src, dst, anchor), which turns "did A link
-- to B" into a query over duplicates for no reader who's asked for one yet.
CREATE TABLE links (
    src_id      BIGINT NOT NULL REFERENCES urls (id),
    dst_id      BIGINT NOT NULL REFERENCES urls (id),
    anchor_text TEXT,
    PRIMARY KEY (src_id, dst_id)
);

-- One row per fetch attempt: status_code and duration_ms, for diagnosing
-- *which* attempt did what. urls.attempts is the hot counter claim_batch
-- bumps in the same statement as the lease; this is the append-only detail
-- an aggregate can't hold without becoming multi-valued.
CREATE TABLE fetch_attempts (
    id          BIGSERIAL PRIMARY KEY,
    url_id      BIGINT NOT NULL REFERENCES urls (id),
    attempt_no  INT NOT NULL,
    status_code INT,
    duration_ms INT,
    error_kind  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX fetch_attempts_url_id_idx ON fetch_attempts (url_id);
