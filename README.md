# Web Crawler

Given a seed URL, discovers and downloads a site's HTML, images, video, and
PDFs, staying within the seed's host, surviving fetch failures and a crash
mid-crawl, and picking up where it left off on restart.

## Running it

```
docker compose up --abort-on-container-exit
docker compose run --rm crawler stats
```

Brings up Postgres, a fake fetch API, and the crawler; the crawler exits
once the frontier drains. `tests/fake_api/` is a test double for offline
development, not an implementation of the fetch service the assignment
specifies.

## Key decisions

**Frontier: Postgres, not a queue.** `FOR UPDATE SKIP LOCKED` claims a url
atomically; a `lease_token` fences every terminal write against the claim
that made it, so a worker whose lease was reclaimed can't overwrite
whoever claims the row next — its write is rejected and logged instead.
Not theoretical: a live double-claim during development (a lease reclaimed
while a worker was still genuinely working it — see "what I'd do
differently") produced a real `"mark_done: lease race lost, no row
updated"` line, and the crawl stayed correct. Rejected a Redis queue: a
lease split still needs to re-check Postgres before re-fetching, so Redis
would add a hop it can't remove.

**Rate limiting: one shared AIMD bucket**, keyed to the fetch gateway, not
one per host — the crawler only ever talks to the gateway, and every
429/Retry-After is a signal about it, not about any origin behind it;
independently-paced per-host buckets could sum above one aggregate limit.

**Handlers: a registry keyed on canonical content type**, sniffed against
the body's own magic bytes, never the declared header or the URL. Not a
plugin loader — a fifth type is a new file, its `@register`, and one
import line in `handlers/__init__.py`; no edits to any existing handler.

**Failures: a closed set of outcomes, classified once.** 404/403 are
permanent, 429/500 are temporary with jittered backoff. A `3xx` —
undocumented in the assignment's own statusCode table — is permanent too,
not a follow-loop target and not just another retry: it's a deterministic
answer about one url, not gateway flakiness, so retrying it meets the
identical redirect every time.

**Storage: sha256-prefixed filenames, one tree per type**, not a
content-addressed store hardlinked into a browsable one — hardlinks break
across filesystems and misbehave under Docker on Windows, the required
platform.

**Change detection: content hash on revisit**, not a conditional request —
the API's status set has no 304, so a conditional request can't save
bandwidth; the body always comes back in full regardless. `ETag` is stored
and sent as `If-None-Match` anyway, since checking costs nothing and a
service that does honor it would otherwise be ignored for free. Real
numbers from two consecutive `docker compose` crawls against the same
fixture graph. The crawler doesn't revisit anything on its own schedule —
there's no re-crawl timer — so run 2 was forced by hand, resetting every
url's `status` to `pending` between runs:

| metric | run 1 (fresh) | run 2 (revisit) |
|---|---|---|
| done / failed | 11 / 6 | 11 / 6 |
| attempts (retried) | 31 (14) | 48 (31) |
| bytes: text/html | 1,143 | 1,190 |
| dedup | 9 distinct / 11 stored (18.2%) | 9 distinct / 11 stored (18.2%) |
| content changed | 0 | 1 — `/drifting` |

The reset didn't zero `attempts` — it's the same counter carrying forward,
not two independent crawls — so run 2's total includes the four
intentionally-flaky fixture routes picking up their retry budgets where
run 1 left them, not thrash from the revisit itself. Only the url whose
body actually differed registers a change; everything else's hash matches
and only `last_seen_at` moves.

Full reasoning for these and several more is in `DESIGN.md`.

## Trade-offs

**No message queue, no cache.** Postgres alone holds frontier and dedup
state — a queue alongside it means two writes for one fact, and a crash
between them either loses a url or repeats it. A cache buys nothing here:
with no 304 to make a conditional hit worth serving, there's no repeat
read to avoid.

**Two spellings of one URL stay two URLs.** Fragment, scheme/host casing,
`../`, and percent-encoding get normalized; query parameters don't get
reordered. Rejected sorting them: it would silently merge urls that might
serve different content. Not sorting costs one redundant fetch, which
content-hash dedup then collapses to a single stored file.

**The wire format is a guess.** The assignment's own `Buffer` type has no
declared JSON encoding, so the base64 guess lives at exactly two call
sites — `fetch/client.py`'s decode, `tests/fake_api`'s encode — each
verified against nothing but itself. If the real API spells the body
differently, those two change; nothing downstream does, since everything
below `FetchResponse` only ever knows `bytes | None`.

## At production scale

Data volume: `claim_batch`'s `pending`-only index scan is what keeps a
claim cheap as `done` rows pile into the millions — confirmed with
`EXPLAIN (ANALYZE, BUFFERS)` at 50k/2k rows, not load-tested past that.
The output tree itself — a flat directory per type, no index file alongside
it — stops being enough past a few million objects; `urls.content_hash` in
Postgres is what already maps a stored file back to the URLs that produced
it, so moving the blob store to S3/GCS at that point is addressed by the
same content hash, with Postgres keeping only the pointer.
Reliability wants the lease-renewal fix below: without it, more
concurrency just means more chances for a live worker's real processing
time to outrun its fixed lease deadline, which it has no way to extend.
Cost is set by the fetch gateway's own rate limit, not by this crawler's
compute: the AIMD limiter already paces to whatever the gateway grants,
so adding workers past that ceiling doesn't move throughput, only queues
faster against it.

## What I'd do differently

**A heartbeat, or periodic lease renewal.** There's no way today for a
worker to prove it's still alive before its lease expires — `lease_seconds`
bounds a crashed worker's claim, but can't tell a crashed worker from a
slow one, since nothing lets a live one renew. A legitimately slow fetch
gets reclaimed exactly like a dead worker's would; `lease_token` fencing
keeps the outcome correct (the loser's write is dropped, logged, never
overwrites) but the fetch runs twice for nothing. Found empirically, not
designed for: the SIGKILL resumability test needed a lease six times
longer than the crash-recovery story alone would justify, specifically to
stop a live worker under real concurrent load from tripping this.

**`<base href>` and a second raster format.** One function, and a new
file plus its `@register` and an import line, respectively — deferred
because nothing in the fixture graph needed them, not because either is
hard.

Full decision log: `DESIGN.md`. AI development record: `AI_WORKLOG.md` and
`ai-transcripts/`.
