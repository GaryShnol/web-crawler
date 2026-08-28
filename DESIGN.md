# DESIGN.md

## Frontier: Postgres, not Redis

Chose `FOR UPDATE SKIP LOCKED` for claims. Rejected a Redis queue (sorted
set + lease): a lease split still needs reclaiming to re-check Postgres
before re-fetching, or a worker dying between its Postgres commit and its
Redis ack causes an unbounded re-fetch against the rate-limited gateway —
once every reclaim needs that check anyway, Redis adds a hop it can't
remove. Would reconsider given evidence SKIP LOCKED is the actual
bottleneck, not assumed contention.

## Rate limiting: one shared token bucket

Chose a single bucket keyed to the fetch gateway. Rejected per-target-host
buckets: the crawler only talks to the gateway, so every 429/Retry-After
describes gateway state, not per-origin state, and independently-paced
sub-buckets can sum above one aggregate limit. Would reconsider if the
gateway ever returns a per-origin signal.

## Off-host assets fetched, off-host pages not enqueued

Chose img/video/source/embed fetching regardless of host; `a[href]` to
another host stays unqueued. Rejected one host rule for both: the
restriction bounds frontier growth, and assets are leaves — nothing parses
further links out of an image, video, or PDF — so an off-host fetch can't
grow it. Would reconsider if a handler ever extracted outlinks from an
asset type.

## Unmatched content type: skipped, not failed

Chose `skipped` — content_type/content_length kept, body dropped, never
retried — for anything outside the four handlers. Rejected failure:
`a[href]` is type-agnostic by design, so landing outside the handlers is
routine, and a typed element's response can diverge from its tag (SVG
under `img`) without being an error. Would reconsider if wrong-type
responses correlated with a retryable fault.

## Unknowable field: null with a reason, not failure

Chose null `duration_seconds` with a `duration_unavailable_reason` when
`ffprobe` is missing from `PATH` or finds nothing to read. Rejected soft
failure: the handler matched and the video is stored, one field is absent
from the source, not lost by the crawler. No condition identified that
would change this.

## Blob naming: sha256 prefix + slug, one tree

Chose one tree per type, filenames as twelve `sha256` characters plus a
slug from the URL path. Rejected a content-addressed store hardlinked into
a browsable tree: hardlinks break across filesystems and misbehave under
Docker on Windows, where this runs — a mechanism that fails on the
required platform is a latent bug, not a fallback. Would reconsider given
a deployment target guaranteed to run on one hardlink-capable filesystem.

## Redirects: permanent on arrival, not a follow loop

Chose: any `3xx` is `PERMANENT_FAILURE` with its own `ErrorKind.REDIRECT`,
`Location` folded into `Classification.detail` (`urls.error_message`).
Rejected a follow loop with `max_redirects`: the statusCode table has no
3xx row, and nothing has ever demonstrated the real API sending one.
Rejected treating it like any other off-contract status
(`TEMPORARY_FAILURE`, retried): a status genuinely never seen before might
be one more flake worth a wasted retry, but a `3xx` is a deterministic
answer about *this* url, so retrying it meets the identical redirect every
time — that asymmetry is why it alone gets `PERMANENT`. Would reconsider
if the real API ever demonstrably sent one.

The cost of this choice is real, not hypothetical: a site that redirects
for anything routine — trailing-slash normalization, HTTP→HTTPS, a moved
page — can't be crawled to completion, because every url behind a redirect
just fails instead of resolving to what it points at. The argument above
says why that's the chosen trade-off, not that the trade-off is free.

## Assumption: body crosses the wire as base64

Chose base64 text (or `null`) for how a `Buffer | null` body crosses the
wire — the real API's own encoding is unverifiable, so this is a guess.
`fetch/client.py`'s decode and `tests/fake_api/app.py`'s encode each call
`base64.b64*` directly now, guessing independently rather than through a
shared wrapper — CLAUDE.md's "no abstraction with a single implementation"
rule applied to that pair once it had exactly one production call site and
one test-double call site. The trade is real: nothing enforces the two
stay in sync. If the real API sends something else, both call sites
change; everything downstream only ever knows `bytes | None`.

## Retry-After: fractional seconds accepted, RFC forbids them

`parse_retry_after` parses via `float()`, not `int()` — RFC 7231's
`delay-seconds` is `1*DIGIT`, no fraction, and `"0.05"` parses anyway.
Deliberate, not an oversight: the header comes from a service this
assignment itself calls unreliable, and a stricter parser would discard a
usable hint over a spec technicality.

## Lease recovery: a separate sweep, not folded into claim_batch

Chose a periodic `recover_expired_leases` statement. Rejected folding an
expired lease into `claim_batch`'s own `WHERE` as a second, `OR`'d-in
branch: tried that first, and at 50k `done`/2k `pending` rows it forces a
`BitmapOr` across both partial indexes and a `Sort` over all 2000 eligible
rows before `LIMIT` — 66 buffers touched in `EXPLAIN (ANALYZE, BUFFERS)`,
versus 3 for the `pending`-only `Index Scan`, because a `BitmapOr` result
has no order Postgres can push `ORDER BY` into. `recover_expired_leases`
doesn't touch `attempts`, same as `release` — only `claim_batch`'s own
`UPDATE` ever increments it. Would reconsider if `claim_batch`'s
`pending`-only plan ever stops being an ordered index scan on its own.

## No lease renewal: can't tell slow from dead

`lease_seconds` bounds how long a crashed worker's claim stays
uncontested — it can't distinguish a crashed worker from a slow one, since
nothing lets a live worker prove it (no heartbeat, no renewal). A fetch
that legitimately outruns `lease_seconds` gets reclaimed exactly like a
dead worker's, and a second worker claims the same row under a fresh
`lease_token` while the first still holds it; fencing keeps the *outcome*
correct (the loser's write is dropped, logged, never overwrites) but the
fetch runs twice for nothing. Found empirically: `test_resumability.py`
needed `LEASE_SECONDS=6`, not `2`, because a live worker under real
concurrent load occasionally took longer. Not fixed — a heartbeat closes
this; see the README.

## enqueue_many: two statements, not one CTE

Chose `INSERT ... ON CONFLICT DO NOTHING`, then a separate `SELECT` (plus
the `links` insert), over one `WITH` query doing both. Reproduced against
a live Postgres: under Read Committed, a sibling read in the *same*
statement as the insert (an `existing` CTE probing the table directly)
runs against the snapshot taken at statement start, and misses a row a
concurrent transaction commits mid-statement — no id comes back, and its
`links` edge is silently dropped. Splitting the read into its own
statement gives it a fresh, post-commit snapshot, since each statement
under Read Committed gets its own. Under REPEATABLE READ, the same
one-statement version doesn't silently miss the row — it raises SQLSTATE
`40001`, a serialization failure, since that isolation level pins one
snapshot for the whole transaction and Postgres won't quietly answer a
conflict question with data outside it. Louder, but it would mean adding
retry-on-serialization handling this pool doesn't currently need. Would
reconsider if this pool's default isolation level ever moves off Read
Committed.

## Migrations: advisory lock; frontier: none

Chose a global advisory lock around the migration runner. Rejected using
one anywhere in `store/frontier.py`: migrations run once, at boot, with no
per-row unit of work, so coarse-and-rare is the right shape, while the
frontier is continuous and per-row — Postgres's own row locks already give
it the right granularity, and an advisory lock there would serialize every
worker into single file. Would reconsider if a frontier operation ever
needed to span more than one row atomically in a way row locks can't
express.

## Worker exceptions: contained per-url, watched per-process

Chose one `except Exception` around all of `process_one`, recording the
catch as its own `internal_error` `ErrorKind` and letting the worker keep
claiming. Rejected letting a bug propagate out silently (the original
behavior — nothing logged, nothing recorded against the url) and rejected
folding it into an existing `ErrorKind` (that would hide, in
`crawler stats`, the difference between "the remote side is unreliable"
and "this codebase has a bug"). `internal_error` is `TEMPORARY_FAILURE`,
not `PERMANENT_FAILURE`: a wasted retry costs at most `max_attempts - 1`
fetches, while a wrong permanent failure buries a url forever on what
might have been one pool blip. It never reports to the rate limiter — no
response came back, so there's no gateway signal to act on.
`CancelledError` isn't caught (a `BaseException` in 3.12, shutdown passes
through); a second failure while persisting the first also propagates —
the database itself is gone by then. `engine.py` watches
worker/supervisor health separately from crawl outcome for the same
reason: a dead supervisor stops lease recovery silently otherwise.

## Drain detection: its own timer, and why the check needs no lock

Chose a dedicated `_watch_drain` task polling `frontier.crawl_complete` on
`drain_check_interval_seconds` (2s default). Rejected checking inside
`_supervise` on `lease_recovery_interval_seconds` (`lease_seconds / 2`,
60s default) — two unrelated questions sharing one timer, and coupling
them meant a two-second crawl could still take 60 more to exit, reading as
a hang under `docker compose up`. Would reconsider if `crawl_complete`
stopped being a cheap existential check.

The check itself needs no lock. Invariant: a `pending`/`in_progress` row
can only spawn new `pending` rows in the same commit that ends its own
`in_progress` status — `enqueue_many` and `mark_done` run inside one
`conn.transaction()` in `_persist_success`, the only place urls are
discovered. Consequence: there's no snapshot where a row reads terminal
but the links it produced don't exist yet, so "no `pending`, no
`in_progress`" is a true fixed point the instant it's observed. Fragility:
this holds by convention only — nothing in the schema or type system
enforces it, and no test would catch a future call site that enqueues
outside that transaction.

## error_message: computed where the specifics live, not reconstructed downstream

Chose an optional `detail` field on `Classification`, filled in only by
classifiers that hold real numbers or a caught exception — declared vs.
actual bytes, the cap and what was read, an exception's own class name.
Rejected building these strings in `worker.py`'s `_persist_failure` by
switching on `error_kind`: that's the persist layer re-deriving a fact the
classifier already had for free, growing by one special case per kind.
`FetchResult.error_detail` carries a fetch-level classifier's `detail`
through the same way `error_kind` already does — the one thing a caller
recording a failure can't safely re-derive once the response is gone.
Kinds with nothing to add leave `detail` `None`.

## Change detection: content hash on revisit, not a conditional request

The API's status set has no `304`, so a conditional request can't save
bandwidth here — the body comes back in full regardless, and the only
honest comparison is of the bytes themselves. `ETag` is stored and sent as
`If-None-Match` anyway: checking costs nothing, and a service that does
honour it would otherwise be ignored for free.

Measured across two consecutive `docker compose` crawls of the same fixture
graph. The crawler has no re-crawl timer, so run 2 was forced by hand,
resetting every url's `status` to `pending` between runs:

| metric | run 1 (fresh) | run 2 (revisit) |
|---|---|---|
| done / failed | 11 / 6 | 11 / 6 |
| attempts (retried) | 31 (14) | 48 (31) |
| bytes: text/html | 1,143 | 1,190 |
| dedup | 9 distinct / 11 stored (18.2%) | 9 distinct / 11 stored (18.2%) |
| content changed | 0 | 1 — `/drifting` |

The reset didn't zero `attempts` — it's the same counter carrying forward,
not two independent crawls — so run 2's total includes the four
intentionally-flaky fixture routes picking up their retry budgets where run
1 left them, not thrash from the revisit itself. Only the url whose body
actually differed registers a change; everything else's hash matches and
only `last_seen_at` moves.
