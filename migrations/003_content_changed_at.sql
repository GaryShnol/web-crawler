-- Tracks when a revisit's body actually differs from what was already on
-- the row, instead of mark_done silently overwriting content_hash every
-- time. Null until the first real change; a same-hash revisit or a url's
-- first-ever successful fetch never touches it. A column, not a
-- content_changes table: there's no runs table, so "which urls changed
-- between two points in time" is content_changed_at > that boundary either
-- way, and a history table nothing queries is dead weight.
ALTER TABLE urls ADD COLUMN content_changed_at TIMESTAMPTZ;
