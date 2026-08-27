# DESIGN.md

## Frontier: Postgres, not Redis

Chose `FOR UPDATE SKIP LOCKED` for claims. Rejected a Redis queue (sorted
set + lease) for faster claiming. A lease split needs reclaiming to
re-check Postgres before re-fetching — otherwise a worker dying after its
Postgres commit but before its Redis ack causes an unbounded re-fetch
against the rate-limited gateway. Once every reclaim needs that check,
Redis adds a hop it can't remove. Would reconsider given evidence SKIP
LOCKED is the actual bottleneck, not assumed contention.

## Rate limiting: one shared token bucket

Chose a single bucket keyed to the fetch gateway. Rejected per-target-host
buckets. The crawler only talks to the gateway, so every 429/Retry-After
describes gateway state, not per-origin state. Splitting by host partitions
one singly-metered resource along a dimension the API gives no signal on;
if the limit is on aggregate volume, independently-paced sub-buckets can
sum above it. Would reconsider if the gateway starts returning a per-origin
signal.

## Off-host assets fetched, off-host pages not enqueued

Chose img/video/source/embed fetching regardless of host; `a[href]` to
another host stays unqueued. Rejected one host rule for both. The
restriction bounds frontier growth; assets are leaves — nothing parses
further links out of an image, video, or PDF — so an off-host fetch can't
grow it. Would reconsider if a handler ever extracted outlinks from an
asset type.

## Unmatched content type: skipped, not failed

Chose `skipped`: content_type/content_length kept, body dropped, never
retried, for anything outside the four handlers. Rejected failure. `a[href]`
is type-agnostic by design — it links to arbitrary downloads, not just
next-pages — so landing outside the handlers is routine. A typed element's
response can also diverge from its tag (SVG under `img`, an HLS manifest
under `video`). Neither is an error. Would reconsider if wrong-type
responses correlated with a retryable fault.

## Unknowable field: null with a reason, not failure

Chose null dimensions with a reason for an SVG lacking width/height/viewBox
— same shape as a missing ffprobe duration. Rejected soft failure. The
handler matched and the content is stored; one field is absent from the
source, not lost by the crawler. No condition identified that would change
this.

## Blob naming: sha256 prefix + slug, one tree

Chose one tree per type, filenames as twelve `sha256` characters plus a
slug from the URL path. Rejected a content-addressed store hardlinked into
a browsable tree. Hardlinks break across filesystems and misbehave under
Docker on Windows, where this runs — a mechanism that fails on the required
platform is a latent bug, not a fallback. Would reconsider given a
deployment target guaranteed to run on one hardlink-capable filesystem.

## Redirects: resolved_url, not a follow loop

Chose reading `Location` off a `200` straight into `FetchResult.resolved_url`.
Rejected a follow loop with `max_redirects` and cycle detection. The fetch
API's status set is closed at `200|404|429|403|500` — no 3xx exists in it —
so the only shape a redirect can take is a `200` the API already followed on
our behalf, telling us where it landed. There's nothing left for this client
to chase, so `max_redirects` came out of config; nothing read it. Would
reconsider if the API ever returned a 3xx directly.

## Assumption: body crosses the wire as base64

The fetch API's own spec is `body: Buffer | null` — that's the real API's
contract, not something I can call and inspect. I don't know how it actually
serializes a `Buffer` into the JSON envelope, so `fetch/client.py` assumes
base64 text, or `null`. `tests/fake_api` encodes the same way *by calling the
same function* (`crawler.models.encode_body`, decoded back with
`decode_body`) rather than each side independently deciding "base64 sounds
right" — the fixture and the client verifying each other would just be two
guesses agreeing with themselves.

If the real API turns out to send something else — a plain string, an array
of ints, hex — the one line that changes is `decode_body` in
`src/crawler/models.py`. Everything downstream (`classify`, `FetchResponse`,
the handlers) already only knows `bytes | None`; none of it knows or cares
how those bytes were spelled on the wire.

## Lease recovery: a separate sweep, not folded into claim_batch

Chose a periodic `recover_expired_leases` statement. Rejected folding an
expired lease into `claim_batch`'s own `WHERE` as a second, `OR`'d-in
branch (`status = 'in_progress' AND lease_until < now()`) — tried that
first, and it's wrong under load. With 50k `done` rows and 2k `pending`,
`EXPLAIN (ANALYZE, BUFFERS)` on the `OR` version shows `BitmapOr` across
both partial indexes, feeding a `Bitmap Heap Scan` that materializes all
2000 eligible rows, then a `Sort` on `next_attempt_at` over that whole set,
*then* `LIMIT`. Dropping the `OR` back to `status = 'pending'` alone
changes the plan to `Index Scan using urls_pending_idx`, ordered, reading
only enough rows to satisfy `LIMIT` (confirmed: 3 buffers touched instead
of 66, in `EXPLAIN`'s own numbers). A `BitmapOr` result has no order
Postgres can rely on, so `ORDER BY` can't be pushed into the scan the way
it can for a single-condition `WHERE` — the cost stops scaling with
`LIMIT` and starts scaling with how many rows are eligible, which is
exactly backwards as `pending` count grows. `recover_expired_leases`
doesn't touch `attempts`, same as `release` — only `claim_batch`'s own
`UPDATE` ever increments it, so a lease reclaimed after a crash still
costs exactly one attempt on its next claim, regardless of which of
`release` / a retry decision / this sweep put the row back in `pending`.
Would reconsider if `claim_batch`'s `pending`-only plan ever stops being
an ordered index scan on its own (e.g. the planner switching strategies at
a different table size) — that's the thing actually being relied on here,
not the absence of an `OR` for its own sake.

## enqueue_many: two statements, not one CTE

Chose `INSERT ... ON CONFLICT DO NOTHING`, then a separate `SELECT` (plus
the `links` insert) as a second statement. Rejected doing both in one
`WITH` query. Verified against Postgres directly: when two transactions
insert the same brand-new URL at the same instant, the loser's own
`INSERT` correctly waits and resolves the conflict once the winner
commits — but a sibling read in the *same* statement (an `existing` CTE
probing the table directly) runs against the snapshot taken at statement
start, before that commit, and never sees the row. Splitting the read into
its own statement gives it a fresh snapshot, taken after the first
statement has already returned — by which point every conflicting insert
it raced against has resolved one way or the other. Read Committed's
per-statement snapshot is what makes this safe; REPEATABLE READ pins one
snapshot for the whole transaction, so the same `INSERT` there raises
"could not serialize access due to concurrent update" instead of silently
missing the row — louder, but it would mean adding retry-on-serialization
handling around this call, which Read Committed doesn't need. Would
reconsider if this pool's default isolation level ever moves off Read
Committed.

The 40001 comes from the same check Postgres applies to any `UPDATE`,
`DELETE`, or `SELECT FOR UPDATE`/`FOR SHARE` that finds its target row was
concurrently changed by a transaction that has since committed — the docs
describe it for that case as "the second updater will get a serialization
failure error." `INSERT ... ON CONFLICT` hits the identical check: deciding
`DO NOTHING` vs. actually inserting is a predicate over the conflicting
row, exactly like an `UPDATE`'s `WHERE` clause is, and evaluating it after
a wait means looking at a row version that didn't exist under the
transaction's original snapshot. Under Read Committed each statement calls
`GetTransactionSnapshot()` fresh — `IsolationUsesXactSnapshot()` is false
— so re-evaluating against the now-committed row is just a normal
statement-scoped re-check, no violation, no error. Under REPEATABLE
READ/SERIALIZABLE, `IsolationUsesXactSnapshot()` is true: the one snapshot
taken at the transaction's first query is reused for every later
statement, so there is no fresher snapshot to fall back to without
breaking the promise that every read in the transaction sees one fixed
point in time — Postgres won't quietly answer the conflict question using
data outside that snapshot, so it raises 40001 and pushes the retry back
to the client instead. This is Postgres's snapshot-isolation "first
updater wins" rule, and `DO NOTHING` was never exempt from it — only a
conflict already visible in the original snapshot (no wait needed) skips
the check entirely, which is the case the docs' "no error" language
actually describes.

## Migrations: advisory lock; frontier: none

Chose a global advisory lock around the migration runner. Rejected using
one anywhere in `store/frontier.py`. Migrations run once, at boot, with no
per-row unit of work — coarse and rare is the right shape. The frontier is
the opposite: continuous, per-row, high-frequency, and Postgres's own row
locks (`FOR UPDATE SKIP LOCKED`, or the lock a plain `UPDATE ... WHERE id
= $1` already takes) already give the right granularity. An advisory lock
around frontier writes would serialize every worker into single file,
which is the concurrency the schema exists to provide. Would reconsider if
a frontier operation ever needed to span more than one row atomically in a
way row locks can't express.

## Worker exceptions: contained per-url, watched per-process

Chose one `except Exception` around all of `process_one`, recording the
catch as its own `internal_error` `ErrorKind` and letting the worker keep
claiming. Rejected letting a bug propagate out of `process_one` (the
original behavior — an unhandled exception silently killed that worker
task, with nothing logged and nothing recorded against the url) and
rejected folding it into an existing `ErrorKind` (that would hide, in
`crawler stats`, the difference between "the remote side is unreliable"
and "this codebase has a bug").

`internal_error` classifies `TEMPORARY_FAILURE`, not `PERMANENT_FAILURE`,
on an asymmetry: a wasted retry costs at most `max_attempts - 1` fetches
on a bounded frontier; a wrong `PERMANENT_FAILURE` buries a url forever on
what might have been one pool blip, and nothing about resuming the crawl
would ever reclaim it. It never reports to the rate limiter — no response
came back, so there's no signal about the remote service to act on; only
`429` and `Retry-After` move that.

`CancelledError` is deliberately not caught — it's a `BaseException` in
3.12, so `except Exception` already passes it through unchanged, same as
shutdown always expected. A second failure while persisting the first
(the fresh `pool.acquire()` for the `internal_error` write itself raising)
also propagates: at that point the pool or the database is gone, there's
nothing url-level left to do, and swallowing it would erase the only
signal that the database is unreachable.

That second case is why `engine.py` watches process health separately
from crawl outcome. A worker task ending with a real exception (its
containment failed, not one url) makes `run()` return `1` — `crawler
stats`, not the exit code, is where a url that gave up after
`max_attempts` shows up. The supervisor gets an `add_done_callback` on top
of that same check: it sets `stop_claiming` (the event SIGTERM already
uses) the moment it dies, because a dead supervisor stops lease recovery
silently — without the callback, that only surfaces once the crawl stalls
with everything leased and stuck, or hangs forever.

## Drain detection: its own timer, and why the check needs no lock

Chose a dedicated `_watch_drain` task polling `frontier.crawl_complete` on
`drain_check_interval_seconds` (default 2s). Rejected doing this check
inside `_supervise`, on `lease_recovery_interval_seconds` (`lease_seconds /
2`, 60s by default) — the original shape. Those are two unrelated
questions sharing one timer: how long a lease may go stale before it's
worth reclaiming has nothing to do with how quickly an empty frontier
should be noticed, and the second is a much cheaper, much more
time-sensitive check — a `NOT EXISTS` over two partial indexes, not a
sweep. Coupling them meant a fixture crawl that finished nine urls in two
seconds could still take up to 60 more to exit, which reads as a hang to
anyone running `docker compose up` and watching identical progress ticks
scroll by. Would reconsider if `crawl_complete` ever stopped being a cheap
existential check — a full table scan on that query would make polling it
every 2s the wrong trade.

Splitting the timer raised the question of whether the check itself needs
new locking to stay correct at a faster cadence — it doesn't, and the
reasoning is worth writing down since "how do you know the drain check is
race-free" is the obvious follow-up question:

`crawl_complete` reads `NOT EXISTS (SELECT 1 FROM urls WHERE status IN
('pending', 'in_progress'))`. The race this has to survive: a worker
finishes parsing a page, is about to enqueue the links it found, and the
drain check runs in that exact window. If the check could see zero
pending/in_progress rows *before* those links land, it would end the
crawl with real work still about to be queued.

It can't, because of where `enqueue_many` is called from. The only path
that discovers new urls is `worker.py`'s `_persist_success`, and there,
`enqueue_many` and `mark_done` run inside the same `conn.transaction()` as
each other — the row's move out of `in_progress` and the insertion of
whatever it linked to commit together or not at all. Under Postgres's
default Read Committed isolation, a concurrent statement (the drain
check's `SELECT`) either sees the pre-commit snapshot — the row is still
`in_progress`, so `crawl_complete` is false regardless of the links not
existing yet — or the post-commit one, where both the status flip and the
new `pending` rows are visible together. There's no snapshot in between
where the row reads terminal but the links it produced don't exist yet.
The claim side has the same property for the opposite direction:
`claim_batch`'s `pending → in_progress` transition is one `UPDATE …
RETURNING` statement, so a row is never visible as neither.

So the invariant is: **a `pending`/`in_progress` row can only ever spawn
new `pending` rows as part of the same commit that ends its own
`in_progress` status.** Given that, "no `pending`, no `in_progress`" is a
true fixed point the instant any statement observes it — nothing left
running could still add work without also still holding a row open that
the check would have caught. This is a property of the *codebase*, not of
`crawl_complete` or the schema — nothing in Postgres enforces it, and no
lock makes it true. It holds only because every enqueue happens inside a
persist transaction and nowhere else. Would reconsider — immediately, not
eventually — if a handler or code path ever needs to enqueue outside that
transaction (a fifth handler that discovers links asynchronously, say, or
a retry path that re-queues something mid-flight); that would put a gap
back in the exact place this argument depends on it not existing, and
nothing currently guards against it. No test asserts "every `enqueue_many`
call site runs inside the persist transaction that also ends the row's
`in_progress` status" — the property that makes the drain check safe is
presently upheld by convention, not by the type system or a test.

## error_message: computed where the specifics live, not reconstructed downstream

Chose an optional `detail` field on `Classification`, filled in only by the
classifiers that hold real numbers or a caught exception: `classify`'s
TRUNCATED_BODY branch (declared vs. actual bytes), `classify_oversized_body`
(the cap and what was actually read), `classify_exception` (the exception's
own class name — the only place connect-timeout and read-timeout still
differ once both have collapsed to `ErrorKind.TIMEOUT`), and
`classify_internal_error`/`classify_unparseable_content` (the caught
exception's own `type(exc).__name__: exc` repr). Rejected building these
strings in worker.py's `_persist_failure` by switching on `error_kind` —
that's the persist layer re-deriving a fact the classifier already had for
free, growing by one special case per `ErrorKind` that ever needs one.
`FetchResult.error_detail` carries a fetch-level classifier's `detail`
through the same way `error_kind` already does (see its own docstring): the
one thing a caller recording a failure can't safely re-derive once the
response is gone. The kinds with nothing to add (404, 429, 500, an
unmatched status, an empty body) leave `detail` `None` rather than
restating `error_kind` or a status code `fetch_attempts` already has.
