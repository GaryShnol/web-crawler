# CLAUDE.md

Working notes for this repo. Claude Code loads this file at the start of every
session, so it's where I keep the things I don't want to re-explain each time:
what I'm building, what's already been decided, and how I want the model to
work with me. I update it as decisions land.

## What I'm building

A crawler. Give it a seed URL, it discovers links and follows them across the
site, downloads HTML, images, videos and PDFs, and saves each of them with a
bit of metadata — title and link count for a page, dimensions and size for an
image, size and duration for a video, page count and title for a PDF. Scope is
the seed's host; subdomains are behind a config flag.

The whole thing has to survive the real world: it stays up when fetches fail,
slows down when the service pushes back, runs several workers without tripping
over itself, and can be killed halfway through and picked up again.

I want the smallest code that does that. No abstraction with a single
implementation, no dependency I can't justify, no layer added for a future that
may not come. The hard cases are where the effort goes: races, telling a
permanent failure from a temporary one, a crash mid-crawl, and headers that
lie.

## Stack

Python 3.12 with asyncio, dependencies through uv.

Postgres via asyncpg, raw SQL, no ORM. The two queries that matter here are the
atomic claim and the idempotent insert, and those are exactly the two an ORM
would hide from me.

selectolax for parsing HTML rather than BeautifulSoup — parsing is the only
CPU-bound step in a system that's otherwise waiting on I/O. pillow for images,
pypdf for PDFs, ffprobe for video duration when it's on the PATH.

pydantic-settings, pytest, ruff. Docker Compose brings up Postgres, the fake
API and the crawler together.

No message broker, no cache, no metrics stack. Each of those is a decision I
argue in the README, not something I forgot.

## The fetch API is given

Every fetch goes through one external service. I don't implement it — it's a
black box that's unreliable, rate-limits, and can return different things for
the same URL on different attempts.

```
GET http://mock-api.mock.com/fetch?url=<encoded_url>
-> { statusCode: 200|404|429|403|500, headers: Record<string,string>, body: Buffer|null }
```

The mirror of that type in `src/crawler/models.py` has those three fields and
nothing else. Everything I measure myself — outcome, elapsed time, attempt
number, redirect chain — sits on a separate wrapper type.

Header casing isn't guaranteed, so I read headers case-insensitively. Each one
the spec lists does real work: `Content-Type` picks the handler,
`Content-Length` caps the download and gets checked against what actually
arrived, `Location` is followed and recorded, `Retry-After` drives the rate
limiter, `ETag` is stored and sent back as `If-None-Match`. Content type comes
from the response and is verified against the body's magic bytes — never from
the URL extension.

`tests/fake_api/` is a test double I wrote so I can develop offline. It is not
an implementation of the service.

## Layout

```
src/crawler/  models.py errors.py config.py logging.py url_tools.py
              fetch/    client.py rate_limiter.py retry.py
              store/    db.py frontier.py blobs.py metadata.py
              handlers/ base.py html.py image.py pdf.py video.py
              worker.py engine.py cli.py
tests/        fake_api/  test_*.py
migrations/   001_init.sql
              README.md  DESIGN.md  AI_WORKLOG.md  ai-transcripts/
```

A module gets created when it has work to do, not before.

## Where things stand

Features 1–4 are merged into `main`. `feature/5-hardening` is committed but
not merged: change detection (5.2), observability (5.3), docker-compose and
the Makefile (5.4). 5.1 — the SIGKILL resumability test — was never written.

### What is actually missing

Feature 4 is not finished, and this section claimed otherwise for long enough
that a later session inferred from `git log` that the handlers existed. They
do not. All of the following surfaced from running `docker compose up` for the
first time, not from reading the code:

- **Three of the four handlers don't exist.** There is no
  `handlers/image.py`, `handlers/pdf.py` or `handlers/video.py`, and
  `handlers/__init__.py` imports `html` alone — so every image, PDF and video
  in a crawl lands as `skipped`. Three of the assignment's four required
  content types are never downloaded, processed or persisted.
- **`handlers/html.py` extracts `a[href]` and nothing else.** No `<title>`,
  no `content_metadata` payload (it returns `metadata=None`), no `<base
  href>` handling, and no `src` attributes — which is why images, videos and
  PDFs referenced by `img`/`video`/`embed` never reach the frontier at all.
  Its own module docstring says so.
- **`docker compose up` cannot finish a crawl.** It hangs on the first fetch,
  reproducibly, even at `MAX_CONCURRENCY=1`, and does not reproduce outside
  Docker. Network from inside the stuck container is fine and Postgres has
  nothing blocked, so the two live suspects are (a) connection-pool
  exhaustion — 5.3 added `_progress` as a third `pool.acquire()` consumer
  beside `_supervise` and every worker, so check what `create_pool` derives
  `max_size` from — and (b) the rate limiter starving, since `acquire()` runs
  before any network call and an ungranted token is indistinguishable from a
  fetch that never returns.
- **There is no `README.md`.**

Next, in order: the hang, then the three handlers plus html's missing
extraction, then the two-run change-detection demo, then review and delivery.

### Merged and working

```
models.py    Outcome, FetchResponse(status_code, headers, body),
             FetchResult(outcome, elapsed, resolved_url, response),
             find_header, parse_retry_after, encode_body/decode_body
errors.py    ErrorKind, Classification, classify(response, prev_etag=None),
             classify_oversized_body(), classify_exception(...)
url_tools.py normalize(url, base=None), in_scope(url, seed_host, allow_subdomains)
config.py    Config (pydantic-settings) — seed_url is optional; only
             engine.run() requires it, so `stats` needs no placeholder
logging.py   json to stdout, bindable context, level from config.log_level

fetch/       FetchClient(config) async CM, fetch(url, prev_etag=None) -> FetchResult
             RateLimiter(config, now=monotonic, sleep=asyncio.sleep)
                 acquire(), report(throttled, retry_after), current_rate (property)
             next_attempt(outcome, attempt_no, headers, now, config, jitter=None)
                 -> GiveUp | RetryAt

migrations/  001_init.sql  contents / urls / content_metadata / links /
                           fetch_attempts, plus partial indexes on
                           (next_attempt_at) WHERE status='pending' and
                           (lease_until) WHERE status='in_progress'
             002_skipped_status.sql  urls.status gains 'skipped';
                           urls.lease_token UUID fences terminal writes to
                           the claim that actually made them
             003_content_changed_at.sql  urls.content_changed_at TIMESTAMPTZ

store/db.py  create_pool(config), run_migrations(pool, migrations_dir)
store/frontier.py
             ClaimedUrl(id, url, depth, attempt_no, etag, lease_token)
             DiscoveredLink(raw_url, normalized_url, anchor_text)
             claim_batch(conn, limit, config) -> list[ClaimedUrl]
             mark_done(conn, url_id, lease_token, *, content_type,
                       content_length, content_hash, etag) -> str | None
                 returns the row's own pre-update content_hash (None on a
                 first fetch) via a CTE, and moves content_changed_at in the
                 same statement when that prior hash differs
             mark_unchanged(conn, url_id, lease_token)
             mark_skipped(conn, url_id, lease_token, *, content_type,
                          content_length)
             mark_failed(conn, url_id, lease_token, decision, error_kind,
                         error_message=None)
             record_attempt(conn, url_id, attempt_no, *, status_code,
                            elapsed, error_kind=None)
             release(conn, url_id, lease_token)
             recover_expired_leases(conn) -> int
             crawl_complete(conn) -> bool
             status_counts(conn) -> dict[str, int]
             enqueue_many(conn, links, depth, src_id=None) -> dict[str, int]
store/blobs.py
             write(output_dir, directory, extension, url, body)
                 -> (content_hash, storage_path)
store/metadata.py
             insert_content(conn, content_hash, content_type, byte_size,
                            storage_path)
             insert_metadata(conn, content_hash, kind, payload)
store/stats.py   read-only, nothing in the live crawl calls it
             Stats(status_counts, failure_reasons, attempts_total,
                   urls_attempted, bytes_by_type, dedup_total,
                   dedup_distinct, changed_count)
             gather(conn, since=None) -> Stats

handlers/base.py
             HandlerResult(metadata: dict | None, links: list[DiscoveredLink])
             Handler protocol: kind, directory, extension, sniff(body),
                               handle(body, url) -> HandlerResult
             register(content_type), resolve(content_type, body) -> Handler | None
handlers/html.py  HtmlHandler — kind "page", links only (see above)

worker.py    process_one(...) — one structured "url completed" log line per
             url, carrying outcome (done/skipped/unchanged/retrying/failed)
             plus kind/links/hash_changed or error_kind
engine.py    run(config, sleep=asyncio.sleep) -> int — bounded pool, lease
             supervisor, progress task, graceful shutdown
cli.py       `crawl <seed>` and `stats [--since <iso8601>]` (text report)
tests/       fake_api/ test double + test_* for everything above
```

`attempts` is incremented only by `claim_batch`'s own statement — not by
`release`, not by `recover_expired_leases`, not by `mark_failed`. Nothing in
`store/frontier.py` reads `config.max_attempts`; the give-up decision is
`fetch/retry.py`'s and arrives as an argument. `mark_done` requires a matching
`contents` row to already exist, so the blob is written before it is called.

Config keys: `seed_url` (optional), `database_url`, `fetch_api_url`,
`max_concurrency`, `max_depth`, `requests_per_second`, `max_attempts`,
`lease_seconds`, `max_body_bytes`, `connect_timeout_seconds`,
`read_timeout_seconds`, `allow_subdomains`, `lease_recovery_interval_seconds`,
`poll_interval_seconds`, `shutdown_grace_seconds`, `progress_interval_seconds`,
`rate_limit_min_rps`, `rate_limit_decrease_factor`,
`rate_limit_recovery_successes`, `rate_limit_increase_rps`,
`retry_base_seconds`, `retry_max_seconds`, `output_dir`, `log_level`.
`max_redirects` is gone — see the redirect decision in DESIGN.md.

**Keep this section current at the end of every step.** It is the only thing
standing between a fresh session and a wrong assumption about what exists; the
missing handlers above survived undetected precisely because it went stale.

## Decisions already made

**Postgres holds the frontier.** The hardest requirement is processing each URL
at most once while several workers run. A single `FOR UPDATE SKIP LOCKED`
statement claims work atomically, which means correctness lives in the database
instead of in process memory — so it holds across workers and across a crash
partway through.

**No broker.** A queue alongside Postgres means two writes for one fact, and a
crash between them either loses a URL or does it twice. One source of truth
removes that.

**Files are named by content, inside the type directory.** A file lands at
`output/images/a3f9c2e81b04-hero-banner.png` — the first twelve characters of
the `sha256` plus a slug from the URL path, extension from the detected type.
One tree, not two: it's browsable by type, unique even for query-string URLs,
and readable enough that opening the folder tells you something. I skipped
hardlinking a content-addressed store into a browsable one because that breaks
across filesystems and behaves badly in Docker on Windows, which is where this
has to run. Identical bytes from two URLs are stored once, so the folder alone
can't tell you the site structure — `urls.content_hash` in the database does,
and each type directory also gets an `index.jsonl` mapping file to hash to the
URLs that produced it.

**Two spellings of the same URL stay two URLs.** I normalize the things nobody
argues about — fragment, scheme and host casing, default ports, `../`,
percent-encoding — but I don't reorder query parameters. Sorting them merges
URLs that might serve different content, and that loss is silent and
unrecoverable; not sorting them costs one redundant fetch, which the content
hash then collapses into a single stored file and two rows in `urls`. The
mechanism I already have absorbs the cost of the cautious choice and not the
aggressive one.

**Failures get classified once.** `errors.py` maps status, headers and body to
a small closed set of outcomes: 404 and 403 are permanent and never retried,
429 and 500 are temporary and come back with jittered backoff. Nothing else in
`src/` looks at a status code.

**Rate limiting is AIMD.** One shared token bucket. Honour `Retry-After` when
it's there, cut the rate when a 429 arrives without one, climb back slowly
while things are going well.

**Handlers are a registry keyed on content type.** Adding a fifth type is one
new file and one decorator, with no edits to any existing handler. No plugin
loader — the extensibility should be real and the machinery tiny.

**A conditional request here can't save bandwidth, and I say so.** The status
set has no 304, so the service has no way to tell me "unchanged" without
sending the body. I still store the `ETag` and send `If-None-Match` on a
revisit, because the body type is `Buffer | null` and an empty body on a 200
with a matching ETag is the only shape a conditional hit could take in this
API — if the service honours the header, that costs nothing to support. When
the body comes back in full I fall back to comparing `content_hash`: same hash
means no write and only a `last_seen_at` bump, a different one is recorded as a
change event rather than a silent overwrite. An empty body with no matching
ETag isn't "unchanged" — it's a failure, and it gets its own outcome.

**Off-host assets get fetched; off-host pages don't.** The host restriction gates
frontier growth, not fetch count. An `a href` to another host never gets
enqueued — that's how the crawl stays bounded to the seed's site. `img`,
`video`/`source`, and `embed` targets get fetched regardless of host, because
they're leaves: nothing gets parsed out of an image, a video, or a PDF, so
following one off-host can't grow the frontier. Same restriction, applied to
what it's actually protecting.

**An unmatched content type is skipped, not failed.** `a href` is type-agnostic
by design — it discovers the next page and arbitrary direct downloads alike —
so landing outside the four handlers is routine, not exceptional; a typed
element can return something its tag didn't promise, same as an extension can
lie. Either way, once `Content-Type` plus magic bytes settle outside HTML,
image, video, and PDF, the URL is recorded as `skipped`: no error kind, no
retry, no body written. `content_type` and `content_length` are kept on the
row so what was seen is still visible without having downloaded it.

**A field that's legitimately unknowable isn't a failure either.** An SVG can
be well-formed with no `width`/`height` and no `viewBox` — no intrinsic size
to read, off the root or anywhere else. When that happens `width` and `height`
are stored `null` with a reason, `file_size` is stored as usual, and nothing
about the row says failure. Same shape as video duration when `ffprobe` isn't
on the `PATH`: the content is real and stored, one field is genuinely absent,
and that's a fact worth recording, not an error worth retrying.

If you think one of these is wrong, give me the argument before you change
course.

## What the database keeps

Frontier and visited state, each URL's status — success, failure, or skipped
for an unmatched content type — attempt count and failure reason, its next
retry time and current lease, content hash and ETag, the per-type metadata
(including a reason when an expected field like SVG dimensions or video
duration is legitimately absent), and the discovery graph of which page linked
to which. Enough to stop the crawler, inspect it, and start it again without
losing or repeating work.

## How I want to work

One step per session — I'll say which one at the top.

Ask me the design questions first. Number them, then stop; don't answer them
yourself and carry on. Once I've answered, write only the files for that step.

If a shared type, a public signature, a database column or a config key
changed, finish with a fenced ` ```CONTRACT-UPDATE ` block containing the
replacement text for the affected part of this file. If nothing shared changed,
say `CONTRACT-UPDATE: none`.

A few smaller things: don't read the spec back to me, don't add a dependency
without saying what it replaces, and if what I'm asking contradicts something
above then say so rather than quietly going along with it. If what you're about
to write runs past ~120 lines, stop and tell me what you'd cut — and if I keep
it anyway, that's not the rule failing, it's the rule doing its job: `store/`
modules that are genuinely several single-statement atomic operations (raw SQL,
no ORM) run bigger than that once docstrings are trimmed to a DESIGN.md
pointer each; `frontier.py` is 237 lines across six such functions. Say the
real number and let me decide, rather than reformatting to dodge the count.
No TODOs, no stub functions, and nothing in the test suite is allowed to
actually sleep — inject the clock.

## Commits

Conventional commits, one to three lines of body explaining why. Never `wip`. A
branch per feature, merged with `--no-ff`. No squash, no force push, no
rewriting history that's been pushed — the commit history is part of what gets
reviewed.

Each step ends with the transcript exported to `ai-transcripts/NN-name.md` and
committed alongside the code.

## Checks I run

```bash
uv run ruff check
grep -rn "status_code ==" src/   # should only ever match errors.py
grep -rn "TODO" src/             # should be empty
uv run pytest                    # frontier concurrency test, 20 runs, no flakes
```

And the real one: clone into an empty directory, `docker compose up`, and the
crawl finishes. If it needs a manual step, that's a bug.
