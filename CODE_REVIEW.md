# Pre-submission code review

Staff-eng pass over `junior-mid_assignment_v2.md` against the current tree
(`feature/code-review`, HEAD `af32542`). Every file:line cited below was
read directly in this pass, not carried over from an earlier note. Verified
mechanically too: `uv run ruff check` (clean), `grep -rln "\.status_code"
src/` → only `errors.py` and `worker.py`, `grep -rn TODO src/` (empty),
`uv run pytest -q` (**211 passed**, real Postgres via `tests/conftest.py`).
`docker compose up` itself was **not** re-run in this pass — no code has
changed since the last verified run at commit `4efaa9f` (exit 0, `done: 11,
failed: 6, skipped: 0`, ~14s), and this pass touches no source file, so that
result still stands. No code changed in this stage; this document is the
audit and critique only, feeding a separate FIX pass.

## Status

Fixed, one commit each:

| Finding | Fix | Commit |
|---|---|---|
| C1 — images PNG-only | JPEG/GIF/WEBP handlers, sharing `_raster.raster_metadata` | `c06425d` |
| C2 — `<base href>` not honoured | resolved against it when present, page url otherwise | `c8d42b2` |
| C4 — completion log overstated enqueued links at `max_depth` | `links` now the enqueued count; `links_deferred` added | `27201bf` |
| C7 — `resolve()`'s dispatch had no direct test | `tests/test_handlers_base.py` | `0b53293` |
| C8 — `CLAUDE.md`'s `frontier.py` line count was stale | corrected, and the sweep it prompted | `3b6b9a4`, `0d87b89`, `735e135` |
| C3 — `engine.py` imported `worker.py`'s private `_wait` | moved to a shared, public `asyncio_util.wait` | `2b3b4dc` |

Refused on purpose, not fixed:

- **C5 — no lease renewal.** Fencing already keeps the *outcome* correct
  under a reclaimed lease; adding a heartbeat is a real architectural
  addition (a cooperative renewal task per claim), not a bug fix. Stays
  documented in README.md/DESIGN.md as what's left to do.
- **C6 — redirects not followed.** The documented status set has no 3xx
  row in it; following one reshapes `FetchResult`/`Classification`/the
  frontier write path more than any fix in this pass. DESIGN.md now
  states the cost plainly alongside the argument for the choice.

---

## 1. Traceability audit

Legend: **DONE** — fully meets the requirement. **PARTIAL** — meets it in a
narrower way than the spec implies. **MISSING** — not implemented.
**WRONG** — implemented but incorrect or contradicts the spec.

### Core behavior

| Requirement | Where | Status |
|---|---|---|
| Accept a seed URL, crawl the site it belongs to | `cli.py:96,98` (`Config(seed_url=args.seed)`), `engine.py:207-215` | DONE |
| Discover links, follow them, stay within seed's domain | `handlers/html.py:46-56` (extraction), `url_tools.py:81-88` (`in_scope`), `worker.py:109-113` (gate before enqueue) | DONE |
| Process each URL at most once, even under concurrency | `store/frontier.py:69-123` (`claim_batch`, `FOR UPDATE SKIP LOCKED`, mints a fresh `lease_token` on every claim), every terminal write fenced on that token (`mark_done`/`mark_unchanged`/`mark_skipped`/`mark_failed`/`release`, `frontier.py:172-363`) | DONE |
| Download, process, persist HTML/images/videos/PDFs | `handlers/html.py`, `handlers/image.py`, `handlers/video.py`, `handlers/pdf.py` | **PARTIAL** — HTML/video/PDF handle their whole family; **"images" is PNG only** (`handlers/image.py:14,17-25` — one magic-byte check, one registered content type). Any JPEG on a real crawl sniffs to no handler and lands `skipped`. See §2 C1. |

### Output

| Requirement | Where | Status |
|---|---|---|
| Separate directories by type | `handlers/*.py`'s `directory` field, written via `store/blobs.py:22-38` — `pages/`, `images/`, `videos/`, `pdfs/` | DONE (`pages/` not `html/` — disclosed in README.md:41-44, a naming choice, not a gap: the requirement is "separate directories by type," not that literal string) |
| Collision handling, query strings, traceability | `store/blobs.py:16-38` — sha256-prefix (12 hex chars) + slugged URL path, content-addressed so identical bytes from any URL share one file (idempotent write, `blobs.py:36`); `urls.content_hash` (`migrations/001_init.sql:49`) is the traceability map back from a stored file to every URL that produced it | DONE |

### Per-type processing

| Type | Requirement | Where | Status |
|---|---|---|---|
| HTML | title + link count | `handlers/html.py:70-73` | DONE, but resolution always uses the page's own URL — **`<base href>` is not read at all** (`handlers/html.py:6,54,64`, and pinned as *current* behavior by `tests/test_handlers_html.py:65-74`, whose own comment calls it a later step, not a bug). See §2 C2. |
| Images | width × height, file size | `handlers/image.py:27-31` | PARTIAL, see Core Behavior row above |
| Videos | file size, duration if available | `handlers/video.py:82-87`; duration via `ffprobe`, with a documented-absent-reason fallback when it's missing from `PATH` or has nothing to read (`video.py:27-69`) | DONE |
| PDFs | page count, title if present | `handlers/pdf.py:24-28` | DONE |
| 5th type extensibility, no rewrite of existing handlers | `handlers/base.py:60-92` — registry keyed on `content_type`, `@register`, `resolve()`'s hint-then-sniff-fallback loop | DONE by construction, but **never actually exercised by a second handler sharing a top-level type** — the claim rests on the shape of the code, not a concrete instance of it. The PNG-only gap above is exactly the fixture this claim needs and doesn't have. |

### Persistence & state (database)

| Requirement | Where | Status |
|---|---|---|
| Frontier / visited state | `urls.status` (`migrations/001_init.sql:33-34`, extended by `002_skipped_status.sql:6-8` for `'skipped'`) | DONE |
| Per-URL status + failure reason | `urls.status`, `urls.error_kind`, `urls.error_message` (`001_init.sql:52-53`) | DONE |
| Retry bookkeeping | `urls.attempts`, `urls.next_attempt_at` (`001_init.sql:38,45`), plus append-only `fetch_attempts` (`001_init.sql:93-103`) for per-attempt `status_code`/`duration_ms` | DONE |
| Content metadata | `content_metadata`, keyed by `content_hash` (`001_init.sql:69-73`) | DONE |
| Discovery relationships | `links(src_id, dst_id, anchor_text)`, `PRIMARY KEY (src_id, dst_id)` (`001_init.sql:82-87`) | DONE |
| Content hashes for dedup / change detection | `contents.content_hash` PK (`001_init.sql:8-14`), `urls.content_changed_at` (`003_content_changed_at.sql`), computed inside `mark_done`'s own statement (`frontier.py:172-234`) | DONE — measured across two real crawls, README.md:207-224 |
| Schema reflects real understanding of the problem | `lease_token` fencing (`002_skipped_status.sql:10-18`), partial indexes scoped to the one status each hot query touches (`001_init.sql:60-64`, confirmed against `claim_batch`'s own `WHERE status = 'pending'`) | DONE |

### External services & infrastructure

| Requirement | Where | Status |
|---|---|---|
| Justify every piece of infra, including *not* adding it | README.md:46-49 ("No queue, no cache"), DESIGN.md:1-11 (Postgres-vs-Redis, with a stated condition for reconsidering) | DONE |
| `docker-compose.yml` | `docker-compose.yml` — `db` (healthchecked), `fake-api` (healthchecked, stands in for the real fetch service), `crawler` (depends on both being healthy) | DONE. `Dockerfile`'s `crawler` stage installs `ffmpeg`+`gosu`, drops root via `docker-entrypoint.sh` after chowning the mounted volume — the CRLF-entrypoint and drain-cadence bugs both described in CLAUDE.md as found and fixed are consistent with what's in the tree now (`.gitattributes:1` pins `*.sh` to `eol=lf`; `engine.py:69-88`'s `_watch_drain` runs on its own `drain_check_interval_seconds`, not `_supervise`'s lease-recovery interval) |

### Operational concerns

| Requirement | Where | Status |
|---|---|---|
| Resilience — transient failures don't crash the process | `errors.py:50-165`, closed classification; `worker.py:218-220` contains an uncaught bug per-URL as a retryable `internal_error` instead of killing the worker | DONE |
| Rate control adapting to pushback | `fetch/rate_limiter.py:72-99` — AIMD: `Retry-After` sets a hold, a bare 429 multiplicatively cuts the rate, a success streak (`rate_limit_recovery_successes`) additively recovers it | DONE — unit-tested with an injected clock, no real sleeping (confirmed `now`/`sleep` are constructor params, `rate_limiter.py:23-31`) |
| Concurrency — race-free shared state | `SKIP LOCKED` claim + `lease_token` fencing (above); rate limiter state mutated only under `self._lock` in `acquire()`, and `report()` is deliberately synchronous so it can't be preempted mid-mutation (`rate_limiter.py:59-70,72-75`) | DONE |
| Resumability — stop/resume without loss or duplication | `recover_expired_leases` (`frontier.py:126-145`); proven by a **real SIGKILL of a real OS subprocess** in `tests/test_resumability.py` — polls live DB state for genuine in-flight work before killing (`_wait_then_kill`, lines 108-136), asserts a claimed-but-incomplete URL costs at most one extra attempt, and that at least one URL actually needed that recovery path (`saw_exact_retry`, lines 199-238) | DONE — the single strongest piece of evidence in the suite |
| Observability | structured JSON logs (`logging.py`), one "url completed" line per URL carrying `outcome`/`kind`/`links`/`hash_changed`/`error_kind` (`worker.py:135-142,222`), a periodic "progress" line with measured-vs-permitted rate (`engine.py:91-112`), `crawler stats` (`cli.py:17-59`) | DONE, with one accuracy nit — see §2 C4 |

### Hard edges named explicitly for this review

| Edge | Where | Status |
|---|---|---|
| 200/404/429/403/500, permanent vs. transient | `errors.py:13-14,59-65` — closed sets, one classification point | DONE |
| Content-type from headers, verified against body, never the URL extension | `handlers/base.py:75-92` (`resolve`, hint-then-sniff), every handler's `sniff()` reads magic bytes; `url_tools.py` never touches a path extension | DONE — the fixture graph drives both a lying-header case and a missing-header case end to end (`tests/fake_api/site.py:15,17`, asserted in `test_engine.py:408-418`) |
| Same-domain scoping | `url_tools.py:81-88`, gated in `worker.py:109-113` | DONE — subdomains opt-in (`config.allow_subdomains`); off-host assets are fetched by design (an interpretation, argued in DESIGN.md's "off-host assets" section, not a scope miss — assets don't grow the frontier) |
| Exactly-once under concurrency | see above | DONE |
| Dedup / hashing | `store/blobs.py`, `contents.content_hash` | DONE |
| Retries + backoff | `fetch/retry.py:27-68` — full-jitter exponential (`retry_base_seconds` × 2^(attempt-1), capped at `retry_max_seconds`), `Retry-After` overrides the formula entirely, `max_attempts` ceiling | DONE |
| Adaptive rate limiting | `fetch/rate_limiter.py` | DONE |
| Resumability after a hard kill | `tests/test_resumability.py` | DONE |
| Per-type processing + extensibility | see above | PARTIAL (images) |
| DB schema depth | see above | DONE |
| Output layout + collisions/query strings | see above | DONE |
| Observability | see above | DONE, with C4 below |

### Deliverables

| Requirement | Where | Status |
|---|---|---|
| Complete solution, meaningful commit history | 70+ commits, feature branches merged `--no-ff` (`git log --oneline`), conventional-commit subjects with a why-focused body | DONE |
| README (½–1 page): decisions, trade-offs, production scale, what I'd change | `README.md` | DONE — genuinely short, all four asked-for sections present |
| `AI_WORKLOG.md`: tools, models per stage, why | `AI_WORKLOG.md` | DONE — also documents rejected AI proposals and where the author was wrong, which the assignment explicitly asks for |
| Full AI transcripts | `ai-transcripts/`, one file per step + an index `README.md` | DONE |

**Bottom line:** one real functional gap (images: PNG-only, C1) and one real
correctness gap (`<base href>`, C2) are what would actually cost points on a
literal read of the spec. Everything else in the spec's own hard-edge list
is met. This is a strong submission with two fixable holes, not a shaky one.

---

## 2. Critique

Blunt, ranked by what actually costs points in a hiring review. Nothing
below repeats a plain pass from §1.

**C1 — Images: PNG-only is a functional shortfall, not a style nit.** The
spec says "images," not "PNG." `handlers/image.py:14` sniffs exactly one
magic-byte sequence, and `handlers/base.py:75-92`'s registry has exactly one
entry for the image family. On any real website, most images are JPEG; every
one of them will sniff-fail every registered handler and land `status =
'skipped'` — not attempted, not an error, just silently absent from the
crawl's output. The "adding a fifth content type shouldn't require
rewriting existing handlers" claim in the spec (and echoed in this repo's
own DESIGN.md) has never been exercised by a second handler for the *same*
top-level type — it's an architectural claim resting entirely on the shape
of `base.py`, with no second data point proving the shape actually holds.

**C2 — `<base href>` gap is a correctness bug waiting for the right site,
not a nice-to-have.** `handlers/html.py` resolves every relative `href`/
`src` against the page's own URL (`normalize(href, base=url)` at lines 54
and 64) and never looks for a `<base>` element. Any site that uses one —
common under a path prefix, e.g. GitHub Pages project sites, or any site
served from a non-root mount — gets every relative link resolved to the
wrong absolute URL. It doesn't crash; it enqueues wrong URLs that either
404 or silently land outside the intended scope. The test suite pins this
as *current, deliberate* behavior (`test_handlers_html.py:65-74`'s own
comment: "`<base href>` support is a later step"), which is honest, but it
doesn't change that it's a real gap against "extract... the count of
discovered links" when the count is right and the URLs behind it are wrong.

**C3 — `engine.py` reaches into `worker.py`'s private name.**
`engine.py:34`: `from .worker import _wait`. A leading-underscore import
across a module boundary. Correct today, but nothing stops `worker.py` from
changing `_wait`'s contract on the assumption it's private to that module,
silently breaking `engine.py`'s drain/supervise/progress loops, which all
depend on it (`engine.py:66,88,112`). Low-risk, cosmetic, but it's exactly
the kind of boundary violation a "separation of concerns" grading criterion
is looking for. Fix is trivial: promote it to a small shared utility, or
drop the underscore and own the public contract — this file's own docstring
already treats `_wait` as a piece of shared machinery, not a `worker.py`
implementation detail.

**C4 — the completion log can overstate what was enqueued.**
`worker.py:109-124`: `enqueueable_links` is computed from scope alone;
`within_depth` independently gates whether `enqueue_many` actually runs.
`worker.py:138` sets `context["links"] = len(enqueueable_links)`
unconditionally — so at `max_depth`, the "url completed" log line reports N
links discovered while zero rows were actually enqueued. Not a correctness
bug (the database is right), but exactly the kind of log/DB mismatch that
sends an on-call engineer chasing a "missing enqueue" that was working as
designed. Cheap to fix: gate the reported count on `within_depth` the same
way the enqueue itself is gated, and keep the discovered-but-deferred count
if it's worth keeping at all.

**C5 — no renewal/heartbeat on a held lease (already disclosed, still worth
stating plainly for the interview).** `lease_seconds` (`config.py:21`,
default 120s) can't distinguish a live, slow worker from a dead one —
nothing lets a live worker prove it's still working. `frontier.py:126-145`'s
`recover_expired_leases` reclaims purely on elapsed time. Both README.md and
DESIGN.md say this outright, and `test_resumability.py:78-90`'s own comment
records finding it empirically: `LEASE_SECONDS=6` was needed, not `2`,
because real concurrent load occasionally made a live worker's own
processing time exceed a short lease. Fencing via `lease_token` keeps the
*outcome* correct — the stale write loses the race, logged, never
overwrites — so this is wasted work under load, not a correctness bug. It's
the single sharpest interview question this codebase invites (§3, Q1).

**C6 — redirects are terminal, never followed.** `errors.py:67-76`: any
`3xx` is `PERMANENT_FAILURE` with `ErrorKind.REDIRECT`; nothing ever fetches
`Location`. Defensible — the documented status set (`200|404|429|403|500`)
has no 3xx row at all, and DESIGN.md's argument (a 3xx is a deterministic
answer about *this* URL, so retrying it meets the same redirect every time)
is internally consistent. But it means the crawler cannot complete a crawl
of any site that uses redirects for routine things real sites do
constantly — trailing-slash normalization, HTTP→HTTPS, moved pages. A
reviewer will ask about this regardless of how well-argued the doc is
(§3, Q2).

**C7 — `resolve()`'s dispatch logic has no direct unit test.**
`handlers/base.py:75-92` is the actual registry mechanism — hint lookup,
sniff-fallback loop, no-match case — and it's only exercised indirectly
through `worker.py` integration tests (`test_worker.py`'s `TestSkipped`,
`test_lying_content_type_is_routed_by_body_not_header`). That's enough to
prove today's single-PNG-handler case works, but a regression in the
fallback loop itself — the hint handler tried twice, or the wrong instance
returned when two handlers are registered for overlapping bytes — would
only surface as a wrong `content_metadata.kind` several layers downstream,
in an integration test, not at the unit level closest to the bug. This gap
is structurally connected to C1: a second image handler is exactly the
fixture this test needs, and doesn't have.

**C8 — `CLAUDE.md`'s own line-count claim for `frontier.py` is stale.**
The project's working rules (`CLAUDE.md`, "How I want to work") state a
~120-line budget per module and carve out `frontier.py` as a named
exception: "`frontier.py` is 237 lines across six such functions." The file
is currently 412 lines across eleven functions (`claim_batch`,
`recover_expired_leases`, `crawl_complete`, `status_counts`, `mark_done`,
`mark_unchanged`, `mark_skipped`, `mark_failed`, `record_attempt`,
`release`, `enqueue_many`). Not a code defect — the module is still one
atomic-SQL-operation-per-function, same shape the exception was written
for — but it's a stale number in the project's own source of truth, in a
codebase whose `AI_WORKLOG.md` already confesses to exactly this failure
mode once ("two decisions written down early... were never built, but
stayed in `CLAUDE.md` as fact"). Here it's the reverse direction: code grew
past what the doc says, silently. Worth a one-line correction the next time
this file is touched, precisely because the project holds itself to
noticing this class of drift.

**Not a finding:** dependency list, module layout, SOLID adherence
(registry pattern = OCP, `Handler` Protocol = DIP, `store/frontier.py`
taking `asyncpg.Connection` rather than `Pool` = an actually-enforced
transaction boundary, not a docstring-only one — confirmed by reading every
function signature in `frontier.py`), and the concurrency-under-load test
(`tests/test_frontier_concurrency.py`) are all sound. No SQL string
interpolation anywhere (every query in `frontier.py`/`metadata.py` is
parameterized), no path traversal in blob naming (`blobs.py:13,16-19`'s
slug regex strips to `[a-z0-9]` joined by `-`; `../../etc/passwd` collapses
to `etc-passwd` by hand-tracing `_SLUG_RE`), no dead code or TODOs found by
grep.

---

## 3. Interview questions this codebase invites

1. **"Your lease has no heartbeat — what's the actual cost, and how would
   you fix it?"** (C5) The honest answer: correctness never breaks (fencing
   guarantees the outcome), but a legitimately slow fetch under real
   concurrent load gets reclaimed and refetched — wasted work against a
   rate-limited API, not a wrong result. Fix is a periodic
   `UPDATE ... SET lease_until = ... WHERE lease_token = $1` from the
   in-flight worker itself, which needs a cooperative renewal task per
   claim, not just a longer timeout.

2. **"Why doesn't a redirect actually redirect?"** (C6) The documented
   status set has no 3xx in it at all, so the honest answer is "the spec
   left this undefined, and I chose the reading that a 3xx describes the
   URL rather than the API's reliability." The follow-up an interviewer
   will press on: does that choice let the crawler actually finish crawling
   a realistic site? No — most real sites redirect somewhere, routinely.

3. **"Prove 'at most once' isn't just 'in the common case.'"** The honest
   answer walks through `lease_token` fencing end to end: `claim_batch`
   mints a token every claim regardless of the row's prior value
   (`frontier.py:96-99`), every terminal write matches on `(id,
   lease_token)`, and a losing write is logged and dropped, never
   overwritten (`_warn_if_lost_race`, `frontier.py:57-66`). The sharpest
   proof isn't a unit test asserting the SQL shape — it's
   `test_resumability.py` actually observing a URL cost exactly one extra
   attempt after a real SIGKILL, not zero and not more.

---

## 4. Fix plan (for the next pass, not this one)

| ID | Gap | Fix | Risk |
|---|---|---|---|
| F1 | Images: PNG only (C1) | Add `handlers/jpeg.py` — `JpegHandler`, SOI magic-byte sniff (`\xff\xd8\xff`), same shape as `ImageHandler`, reusing the existing Pillow dependency; register it in `handlers/__init__.py`; wire a JPEG fixture route into `tests/fake_api/site.py` so it's driven end-to-end by the whole-graph test in `test_engine.py`, not just a unit test | Low — additive, the registry is designed for exactly this |
| F2 | `<base href>` not honoured (C2) | `handlers/html.py`: read the first `base[href]`, resolve it against the page's own URL, use that as the base for every subsequent `normalize()` call instead of the page URL; falls back to the page URL when absent | Low — one function, existing tests already pin the no-`<base>` case and need updating, not replacing |
| F5 | Log overstates enqueued links at `max_depth` (C4) | `worker.py`: report `links` as the enqueued count only when `within_depth`; if the discovered-but-deferred count is worth keeping, report it under its own key | Low |
| F7 | `resolve()` has no direct test (C7) | Add `tests/test_handlers_base.py` covering hint match, hint-with-parameters, wrong hint falling back to sniff, no-hint sniff, and no-match; F1's second image handler is what makes the "two handlers, overlapping family" case real instead of hypothetical | Low |
| F8 | `CLAUDE.md`'s stale line count (C8) | One-line correction to the `frontier.py` exception note in "How I want to work" | Trivial |

Not fixed by design, argued in §2, restated so the fix pass doesn't
silently absorb them: **C3** (private import — cosmetic, low-risk, a
follow-up rather than a "smallest change" fix), **C5** (lease renewal —
correctly scoped in README.md as "what I'd do differently," a real
architectural addition, not a bug fix), **C6** (redirect following — the
assignment leaves 3xx genuinely undefined, and changing this reshapes
`FetchResult`/`Classification`/the frontier write path more than the other
fixes here).
