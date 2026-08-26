# AI Work Log

## Tools

Visual Studio Code on Windows, with Claude Code running in the terminal
(v2.1.245, then v2.1.246 from the third session on).

## Models

Claude Sonnet 5 (Claude Pro) at every stage: planning, scaffolding,
implementation, tests, review and refactor.

This wasn't a per-stage decision — it's the model my plan gives me, so there was
nothing to choose between. With a second model available I'd have put the
planning and review sessions on a reasoning model, where arguing against my own
design is the whole point, and kept Sonnet for the implementation steps, which
are bounded and contract-first.

## Method

One session per step, seeded from `CLAUDE.md` and ending with a
`CONTRACT-UPDATE` block I paste back into it. Sessions are conversations, not
single briefs, so the decisions show up in the transcripts as positions I argued
rather than output I accepted.

## Notable rejections

1. **`enqueue_many` as a single `WITH` query.** Under Read Committed both
   sub-statements share the snapshot taken at statement start, so a URL another
   transaction is committing at that instant appears in neither the `INSERT`'s
   `RETURNING` nor the sibling `SELECT` — no id comes back and its `links` edge
   is dropped. Reproduced against a live Postgres, then split into two
   statements so the read takes its own snapshot.

2. **Expired leases reclaimed inside `claim_batch`'s `WHERE`.** At 50k `done`
   and 2k `pending`, the `OR` forces a `BitmapOr` and a `Sort` over all 2000
   eligible rows before `LIMIT` — 66 buffers. `pending`-only is an ordered index
   scan that stops at the limit: 3. Recovery went back to its own statement.

3. **`fetch_attempts` proposed as dead weight.** `urls.attempts` is hot state
   `claim_batch` bumps in the same statement as the lease; per-attempt
   `status_code` and `duration_ms` are a different axis a counter can't hold.

4. **`max_attempts` inside the frontier.** The give-up decision is
   `fetch/retry.py`'s; `mark_failed` takes it as an argument and writes it.

5. **Terminal writes matched on `id` alone.** `claim_batch` returned no owner,
   so a worker whose lease expired and was reclaimed mid-fetch would still
   write `done` over the row's new owner — at-most-once broken by the recovery
   path that exists to preserve it. `claim_batch` now mints a `lease_token` per
   claim, and every write that could land on a row the caller no longer owns
   checks it; zero rows matched is a lost race, logged, not raised.

6. **Structured log context flattened into an f-string.** `_ContextFilter`
   overwrote `record.context` instead of merging, so a call site's own
   `extra={"context": ...}` never reached stdout. The fix offered interpolated
   those fields into the message text, which makes them unqueryable. The filter
   merges now; the call sites stayed structured.

7. **`Pool | Connection` as a parameter type.** Both satisfy `.execute`, which
   is why it typechecks and why it is the wrong type: it hides whether a write
   is the claim's own short transaction or part of the caller's longer one.
   `store/frontier.py` takes a `Connection`; the pool stays at `engine.py` and
   `cli.py`.

## Where I was wrong

I challenged the claim that `INSERT ... ON CONFLICT DO NOTHING` raises a
serialization failure under `REPEATABLE READ`, citing a documentation paraphrase
saying such an insert simply does not proceed. The position was held rather than
conceded, the interleaving re-run with exception introspection, and it returned
SQLSTATE `40001` deterministically. I took the correction.

## Transcripts

Every session is in `ai-transcripts/`, one file per step, unedited.
