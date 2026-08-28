# Pre-submission code review

Staff-eng pass over `junior-mid_assignment_v2.md` against the current tree
(`feature/code-review`, HEAD `4efaa9f`). Verified, not assumed: full test
suite run against a real Postgres (`211 passed`), `ruff check` (clean),
`grep -rn TODO src/` (empty), and a **fresh** `docker compose up
--build --abort-on-container-exit --exit-code-from crawler` against a
volume wiped first with `docker compose down --volumes` — exit 0, `done:
11, failed: 6, skipped: 0` in ~14s wall clock. The repo's own README/
DESIGN.md/AI_WORKLOG.md are unusually candid about known gaps; this review
does not re-praise what they already disclose correctly, and calls out
where a disclosed gap is a real requirement miss versus genuinely
out of scope.

No code changed in this stage. Findings below feed the FIX pass in a
separate set of commits.

---

## 1. Traceability audit

Legend: **DONE** — fully meets the requirement. **PARTIAL** — meets it in
a narrower way than the spec implies. **MISSING** — not implemented.
**WRONG** — implemented but incorrect or contradicts the spec.

### Core behavior

| Requirement | Where | Status |
|---|---|---|
| Accept a seed URL, crawl the site it belongs to | `cli.py:82-83,96`, `engine.py:207-229` | DONE |
| Discover links, follow them, stay within seed's domain | `handlers/html.py:46-56` (extraction), `url_tools.py:81-88` (`in_scope`), `worker.py:109-113` (gate before enqueue) | DONE |
| Process each URL at most once, even under concurrency | `store/frontier.py:69-123` (`claim_batch`, `FOR UPDATE SKIP LOCKED`), fenced by `lease_token` on every terminal write (`frontier.py:172-363`); proven under real contention in `tests/test_frontier_concurrency.py:128-139` (500 urls, 12 concurrent claimers, exactly-once) | DONE |
| Download, process, persist HTML/images/videos/PDFs | `handlers/html.py`, `handlers/image.py`, `handlers/video.py`, `handlers/pdf.py` | **PARTIAL** — HTML/video/PDF are unrestricted within their family; **images means PNG only** (`handlers/image.py:14,25`). A real site is majority JPEG; every JPEG on a real crawl lands as `skipped`, not processed. See §3 finding F1. |

### Output

| Requirement | Where | Status |
|---|---|---|
| Separate directories by type | `handlers/*.py`'s `directory` field, written via `store/blobs.py:22-38` — `pages/`, `images/`, `videos/`, `pdfs/` | DONE (`pages/` not `html/` — a deliberate, disclosed rename in README.md:41-44, not a miss) |
| Collision handling, query strings, traceability | `store/blobs.py:32-38` — sha256-prefix + URL-path slug, content-addressed so identical bytes from two URLs share one file; `urls.content_hash` is the traceability map back to originating URLs (`migrations/001_init.sql:49`) | DONE — tested in `tests/test_blobs.py` (collision, query-string slugging, idempotent rewrite) |

### Per-type processing

| Type | Requirement | Where | Status |
|---|---|---|---|
| HTML | title + link count | `handlers/html.py:70-73` | DONE, but **`<base href>` is not honoured** — resolution always uses the page's own URL (`handlers/html.py:7,54,64`; explicitly tested as *current* behavior in `tests/test_handlers_html.py:65-74`, which documents it as a deferred gap, not an oversight). Real sites use `<base href>`; links under one silently resolve wrong and either 404 or land on the wrong page. See §3 finding F2. |
| Images | width × height, file size | `handlers/image.py:27-31` | PARTIAL, see above (F1) |
| Videos | file size, duration if available | `handlers/video.py:82-87`, ffprobe-backed duration with a documented-absent-reason fallback (`handlers/video.py:27-69`) | DONE |
| PDFs | page count, title if present | `handlers/pdf.py:24-28` | DONE |
| 5th type extensibility, no rewrite of existing handlers | `handlers/base.py:60-92` — registry keyed on `content_type`, `@register` decorator, `resolve()` sniff-fallback loop | DONE by construction — proven concretely by the F1 fix below adding a handler with zero edits to `html.py`/`image.py`/`pdf.py`/`video.py`/`worker.py` |

### Persistence & state (database)

| Requirement | Where | Status |
|---|---|---|
| Frontier / visited state | `urls.status` (`migrations/001_init.sql:33-34`, extended `migrations/002_skipped_status.sql:6-8`) | DONE |
| Per-URL status + failure reason | `urls.status`, `urls.error_kind`, `urls.error_message` | DONE |
| Retry bookkeeping | `urls.attempts`, `urls.next_attempt_at`, plus append-only `fetch_attempts` (`migrations/001_init.sql:93-103`) for per-attempt status/duration | DONE |
| Content metadata | `content_metadata` keyed by `content_hash` (`migrations/001_init.sql:69-73`) | DONE |
| Discovery relationships | `links(src_id, dst_id, anchor_text)` (`migrations/001_init.sql:82-87`) | DONE |
| Content hashes for dedup / change detection | `contents.content_hash` PK (`migrations/001_init.sql:8-14`), `urls.content_changed_at` (`migrations/003_content_changed_at.sql`), computed in `store/frontier.py:172-234` | DONE — two-run measurement table in README.md:207-224 |
| Schema reflects real understanding of the problem | lease fencing via `lease_token` (`002_skipped_status.sql:10-18`), partial indexes scoped to the one status each query touches (`001_init.sql:60-64`) | DONE |

### External services & infrastructure

| Requirement | Where | Status |
|---|---|---|
| Justify every piece of infra, including *not* adding it | README.md:46-49 ("No queue, no cache"), DESIGN.md:1-11 (Postgres-vs-Redis argument with a reproducible reason, not a hand-wave) | DONE |
| `docker-compose.yml` | present, brings up db + fake-api + crawler | DONE — verified with a **fresh** volume in this review (see header); the container previously exited 255 on Linux due to CRLF line endings, since fixed via `.gitattributes` (commit `5277b3d`) and confirmed still fixed |

### Operational concerns

| Requirement | Where | Status |
|---|---|---|
| Resilience — transient failures don't crash the process | `errors.py:50-165` closed classification; `worker.py:218-220` contains an uncaught bug per-URL as a retryable `internal_error` rather than killing the worker | DONE |
| Rate control adapting to pushback | `fetch/rate_limiter.py` — AIMD, honours `Retry-After` as a hold, multiplicative cut on a bare 429, additive recovery after a success streak | DONE — unit-tested with an injected clock (`tests/test_rate_limiter.py`), no real sleeping |
| Concurrency — race-free shared state | `SKIP LOCKED` claim + `lease_token` fencing; rate limiter behind one `asyncio.Lock` (`fetch/rate_limiter.py:37,61`) | DONE |
| Resumability — stop/resume without loss or duplication | `store/frontier.py:126-145` (`recover_expired_leases`), proven with a **real SIGKILL of a real subprocess** in `tests/test_resumability.py` (not simulated — a kill against an in-process asyncio task would prove nothing) | DONE — the strongest single piece of evidence in the suite |
| Observability | structured JSON logs (`logging.py`), one completion line per URL (`worker.py:222`), a periodic progress line with measured-vs-permitted rate (`engine.py:91-112`), `crawler stats` (`cli.py`) | DONE |

### Hard edges named explicitly for this review

| Edge | Where | Status |
|---|---|---|
| 200/404/429/403/500, permanent vs. transient | `errors.py:59-79` | DONE — 404/403 permanent, 429/500 transient, closed set, single classification point (`grep -rln "\.status_code" src/` → only `errors.py` and `worker.py`, and `worker.py`'s only use is reading `result.response.status_code` for the attempt log, never branching on it) |
| Content-type from headers, verified against body, never the URL extension | `handlers/base.py:75-92` (`resolve`), sniffed via magic bytes in every handler's `sniff()`; `url_tools.py` never inspects a path extension anywhere | DONE — the fixture graph deliberately drives a header/body mismatch both ways (`LYING_CONTENT_TYPE`, `MISSING_CONTENT_TYPE` in `tests/fake_api/site.py:15,17`) and both are asserted end to end |
| Same-domain scoping | `url_tools.py:81-88`, `worker.py:112` | DONE — subdomains opt-in (`allow_subdomains`), off-host assets fetched by design (documented, see DESIGN.md's "off-host assets" decision) — this is a scope *interpretation*, not a miss: it doesn't grow the frontier, so it can't break "stays within the seed's domain" |
| Exactly-once under concurrency | see above | DONE |
| Dedup / hashing | `store/blobs.py`, `contents.content_hash` | DONE |
| Retries + backoff | `fetch/retry.py:27-68` — full-jitter exponential, `Retry-After` override, `max_attempts` ceiling | DONE |
| Adaptive rate limiting | `fetch/rate_limiter.py` | DONE |
| Resumability after a hard kill | `tests/test_resumability.py` | DONE |
| Per-type processing + extensibility | see above | PARTIAL → fixed in this pass (F1) |
| DB schema depth | see above | DONE |
| Output layout + collisions/query strings | see above | DONE |
| Observability | see above | DONE, with one small logging-accuracy nit (§3 F5) |

### Deliverables

| Requirement | Where | Status |
|---|---|---|
| Complete solution, meaningful commit history | 60+ commits, feature branches merged `--no-ff`, conventional-commit messages with a "why" body | DONE |
| README (½–1 page): decisions, trade-offs, production scale, what I'd change | `README.md` | DONE — genuinely half a page, not padded |
| `AI_WORKLOG.md`: tools, models per stage, why | `AI_WORKLOG.md` | DONE — includes rejected AI proposals and where the author was wrong, which the assignment explicitly asks for ("what you reject... are engineering decisions like any other") |
| Full AI transcripts | `ai-transcripts/`, one file per step + an index README | DONE |

**Bottom line:** one real functional gap (F1, images), one real correctness
gap (F2, `<base href>`), and a handful of critique-level issues below —
nothing else in the spec's hard-edge list is missing or wrong. This is a
strong submission; the fixes in §4 close the only two items that would
cost points on a literal read of the spec.

---

## 2. Critique

Blunt, ranked by what actually costs points in a hiring review. No praise
padding — where something is simply correct, it's in §1, not here.

**C1 — Images: PNG-only is a real functional shortfall, not a style nit.**
The spec says "images," not "PNG." On any real website the crawler will
mark the majority of its images `skipped` (not even attempted) because
`handlers/image.py` only sniffs the PNG magic bytes. The registry pattern
that's supposed to make this a non-issue ("adding a fifth content type…")
has never actually been exercised by a second handler for the *same*
top-level type — it's asserted, not demonstrated. Fixed in §4 (F1).

**C2 — `<base href>` gap is a correctness bug waiting for the right
fixture, not a nice-to-have.** A crawler that resolves every relative link
against the page's own URL instead of a `<base>` tag will silently
mis-resolve links on any site that uses one (common on sites hosted under
a path prefix, e.g. GitHub Pages project sites). It doesn't crash — it
just enqueues wrong URLs that go on to 404 or land in the wrong scope.
Fixed in §4 (F2).

**C3 — `engine.py` reaches into `worker.py`'s private `_wait`.**
`from .worker import _wait` (`engine.py:34`) imports a
leading-underscore name across a module boundary. It's correct today, but
it's an encapsulation leak: nothing stops `worker.py` from changing
`_wait`'s contract on the assumption it's private to that module, silently
breaking `engine.py`. Not touching this in the FIX pass — it's cosmetic,
low-risk, and outside the MISSING/PARTIAL/WRONG scope this review is
fixing; worth a follow-up (promote to a small shared `_asyncio_util.py`,
or drop the underscore and own the public contract).

**C4 — `worker.py`'s completion log can overstate what was enqueued.**
`_persist_success` (`worker.py:135-139`) sets
`context["links"] = len(enqueueable_links)` unconditionally, but the
actual `enqueue_many` call is gated on `within_depth`
(`worker.py:123-124`). At `max_depth`, the completion log line claims N
links discovered while zero new rows were enqueued — a minor
observability inaccuracy (the number in the log doesn't match the
database). Fixed in §4 (F5) since it's cheap and it's exactly the kind of
mismatch that wastes an on-call engineer's time diagnosing a "missing
enqueue" that was actually working as designed.

**C5 — No renewal/heartbeat on a held lease (already disclosed, still
worth restating plainly).** `lease_seconds` can't distinguish a live,
slow worker from a dead one — DESIGN.md and README.md both say so, and
`test_resumability.py`'s comment explains `LEASE_SECONDS=6` was chosen
empirically because 2s flaked under real load. This is the single
sharpest interview question this codebase invites (see §5, Q1). Fencing
keeps correctness intact (a stale write loses the race, never overwrites),
so this is a performance/waste concern, not a correctness one — not fixed
here, correctly scoped as "what I'd do differently" in README.md:73-77.

**C6 — Redirects are terminal, never followed.** Any 3xx is
`PERMANENT_FAILURE` with no retry and no second fetch of `Location`
(`errors.py:67-76`). This is defensible — the documented status set has no
3xx at all — but it means the crawler cannot actually complete a crawl of
a site that uses redirects for anything routine (trailing-slash
normalization, HTTP→HTTPS, moved pages), which real sites do constantly.
The DESIGN.md argument (a 3xx is deterministic, so retrying it is
pointless) is correct on its own terms but sidesteps the more obviously
useful behavior: follow it once and process the target. Not fixed here —
changing this reshapes `FetchResult`/`Classification` and the frontier
write path more than a "smallest change" fix justifies, and the assignment
truly does leave 3xx undefined. Flagged because a reviewer will ask about
it regardless (§5, Q2).

**C7 — Minor: `tests/fake_api/` local `__pycache__` contains stale
artifacts (`jpeg.cpython-312.pyc`, `test_handlers_jpeg...pyc`) from a
prior local experiment that was never committed.** `.gitignore` excludes
`__pycache__/`, so a fresh clone never sees this — verified with `git log
--diff-filter=A -- '*jpeg*'` (empty) and `git status` (clean). No action
needed; noted only so it isn't mistaken for an untracked-file problem
during review. (It also means this review's own F1 fix — adding a real
`jpeg.py` — isn't reusing anything from that stale cache; written fresh.)

**C8 — Test coverage is genuinely strong, with one gap worth naming.**
`handlers/base.py`'s `resolve()` — the actual registry/dispatch logic
(content-type hint, sniff-fallback loop, no-match case) — has no direct
unit test; it's only exercised indirectly through `worker.py` integration
tests (`tests/test_worker.py`'s `TestSkipped`,
`test_lying_content_type_is_routed_by_body_not_header`). That's enough to
prove it works today, but a regression in the fallback loop itself (e.g.
the hint handler being tried twice, or a match returning the wrong
instance when two handlers are registered for overlapping bytes) wouldn't
be caught at the unit level, only by an integration test noticing the
wrong `content_metadata.kind`. Added a direct test in §4 alongside the
JPEG handler, since two registered image-family handlers is exactly the
scenario that would have caught this.

**Not a finding:** dependency list, module layout, SOLID adherence
(registry = OCP, `Handler` Protocol = DIP, `store/frontier.py` taking a
`Connection` not a `Pool` = an actually-enforced boundary, not just a
docstring one), and the concurrency test suite are all sound. No dead
code, no unjustified dependency, no SQL string-formatting (`grep`
confirms every query is parameterized), no path-traversal in blob naming
(slug regex strips to `[a-z0-9-]`, verified by tracing `../../etc/passwd`
through `_slug()` by hand: collapses to `etc-passwd`).

---

## 3. Fix plan (implemented in the next set of commits)

| ID | Gap | Fix | Risk |
|---|---|---|---|
| F1 | Images: PNG only (C1) | Add `handlers/jpeg.py` — `JpegHandler`, SOI magic-byte sniff (`\xff\xd8\xff`), same shape as `ImageHandler`, reusing the existing Pillow dependency. Register it in `handlers/__init__.py`. Wire a JPEG fixture route into `tests/fake_api/site.py` so it's driven end-to-end by the whole-graph test, not just a unit test. | Low — additive, registry pattern designed for exactly this |
| F2 | `<base href>` not honoured (C2) | `handlers/html.py`: read the first `base[href]` node, resolve it against the page's own URL, use that as the base for every subsequent `normalize()` call instead of the page URL. Falls back to the page URL when absent, same as today. | Low — one function, well-isolated, existing tests pin the no-`<base>` case |
| F5 | Log overstates enqueued links at `max_depth` (C4) | `worker.py`: report the enqueued count only when `within_depth`; report the discovered-but-not-enqueued count separately so the information isn't lost, just correctly labeled. | Low |

Each lands as its own commit with tests, per the "small, reviewable
commits" instruction. No new dependency, no external service — flagged in
advance per the rules for this review; none turned out to be needed.

Not fixed, by design (see §2 for why): C3 (private import), C5 (lease
renewal), C6 (redirect following).
