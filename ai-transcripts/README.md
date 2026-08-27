# Transcripts

One file per step, per `CLAUDE.md`'s own workflow — a session names its step
at the top, gets asked design questions before any code, and ends with a
`CONTRACT-UPDATE` pasted back into `CLAUDE.md`. That shape is why these read
as conversations rather than single briefs: a design question gets argued,
not just answered, and the argument sometimes moves the decision.

Three examples, not the only ones. In `01-decisions.md`, a case for Redis
over Postgres for the frontier was built out in real detail — then traced
through its own failure mode and shown to invert: closing the duplication
window Redis was supposed to prevent requires re-checking Postgres before
every re-fetch anyway, at which point Redis has nothing left to do. Postgres
alone, no caveat. In `07-crawl-engine-content.md`, the user caught the most
consequential bug in the project this way: terminal writes weren't fenced
to the claim that made them, so a worker whose lease had already been
reclaimed could still overwrite a second worker's result — breaking
"processed at most once" outright. That's the `lease_token` fencing the
whole design now leans on. And it runs the other way too: in
`11-review-fixes-and-delivery-path.md`, the user reported a docker-compose
hang and a confident diagnosis for it; rather than writing the requested
fix, the assistant reproduced it first, found the crawl actually
self-terminated correctly, and traced the appearance of a hang to a
60-second poll interval the earlier kill happened to land nine seconds
before. The reported finding didn't exist. The user's own words: "you're
right and i was wrong — 7 doesn't exist."

Not every session is like this — several (scaffolding, the handler
build-out) are mostly straightforward implementation against a short list
of design questions, with little to argue about. That's fine; the point
isn't conflict for its own sake, it's that what shipped is whatever
survived being questioned, not whatever was typed first.

One gap worth naming: two real, committed pieces of work have no transcript
here — the `.gitattributes` CRLF fix for `docker-entrypoint.sh`, and the
whole `feature/6-delivery` review-findings session this table was written
during (content-type/redirect/`encode_body` fixes, fixture coverage, the
SIGKILL resumability test, `DESIGN.md`'s trim, and this README). Both are
real commits; neither has been exported to a transcript file yet.

| file | step | what got decided |
|---|---|---|
| `01-decisions.md` | gap analysis + early design arguments | Audited `CLAUDE.md` against the assignment spec, closed two real gaps: off-host assets get fetched regardless of host (they're leaves, can't grow the frontier) while off-host *pages* stay unqueued; an unmatched content type is recorded `skipped`, not failed. Argued Redis vs. Postgres for the frontier in full (see above) and kept Postgres. Flagged a proposed per-type `index.jsonl` index as likely over-engineering — "the jsonl file is the one I'd actually cut." |
| `1.2-decisions.md` | first `DESIGN.md` draft | Distilled `01`'s arguments into the original `DESIGN.md` (six sections, ~400-word budget). The assistant overreached the source material twice in the same session — writing an unauthorized "Cut: index.jsonl" section as though it were a settled decision (it wasn't; `index.jsonl` stayed undecided, never actually cut), then dropping the real blob-naming decision to make room for it — both caught and corrected. |
| `02-scaffolding.md` | project scaffolding | `pyproject.toml`, dependencies, `models.py`/`config.py`/`logging.py`, a verified multi-stage Dockerfile (non-root `appuser`, apt `ffmpeg`, a dependency layer that stays cached across code changes). Mostly design questions answered directly rather than argued; the one real call was stdlib `logging` (`contextvars` + a JSON formatter) over `structlog` as unjustified for this scope. |
| `03-errors.md` | error classification (`errors.py`) | Built `classify()`/`classify_exception()` mapping status codes, headers, body, and network exceptions onto a closed `Outcome`/`ErrorKind` set — the "one place a status code gets read" rule. The user overruled a proposed fix for `errors.py` importing `aiohttp` (a translation layer / local exception hierarchy in `client.py`) as indirection with no real payoff; kept a narrow `from aiohttp import ClientConnectionError` instead. |
| `04-fetch-client-rate-limit-retry.md` | `fetch/client.py`, `rate_limiter.py`, `retry.py` | Built the fetch client, the AIMD rate limiter, and retry backoff. Caught a real bug: `max_body_bytes` was enforced after the whole body was already buffered in memory, defeating its own purpose — fixed to stream-and-stop at the cap. Corrected test hygiene (a second, redundant fake HTTP server instead of reusing `tests/fake_api/`; a real-sleeping timeout test; module-level monkeypatching for the clock) into the `now`/`sleep` constructor-injection pattern used everywhere in the suite since. Introduced `encode_body`/`decode_body` and `FetchResult.resolved_url` — both later removed as unused/single-implementation (see `AI_WORKLOG.md`'s "Where I was wrong"). |
| `05-frontier.md` | frontier claim/lease/enqueue logic | `FOR UPDATE SKIP LOCKED` claiming, `enqueue_many`'s two-statement split (verified against a live Postgres: a single combined CTE misses a row a concurrent transaction commits mid-statement), the advisory-lock-for-migrations-not-frontier split, `fetch_attempts` kept as a distinct per-attempt log against `urls.attempts`' hot counter. The user misdiagnosed a Read Committed serialization question, then explicitly conceded and asked for the correct mechanism (SQLSTATE `40001`) written into `DESIGN.md` instead. |
| `06-frontier-tests.md` | frontier concurrency + recovery tests | `test_frontier_concurrency.py` against a real Postgres, no mocks: the actual two-new-parents-same-new-url race `enqueue_many`'s split exists for (not just the already-exists case, which doesn't exercise it), plus lease-expiry crash recovery. Fixed `conftest.py` to skip (not error) when Postgres is unreachable, naming the env var to set. |
| `07-crawl-engine-content.md` | `worker.py`, `engine.py`, `cli.py` | The core pipeline: claim → rate-limit → fetch → route → persist → enqueue → terminal status, one transaction per url. The project's most consequential finding: terminal writes weren't fenced to the claim that made them, so a worker whose lease was reclaimed after expiry could still overwrite a second worker's result, breaking "processed at most once." Fixed with `lease_token` UUID fencing. Also fixed a real logging bug (bound context was being overwritten, not merged) and caught a stale local test database that wasn't representative of a fresh clone. |
| `08-hardening-observability-compose.md` | conditional revisits, structured logging, `docker-compose.yml` | Conditional-revisit logic (`If-None-Match`, content-hash change detection), JSON logging with bound context, the first working compose stack. `docker compose up` appeared to hang; the user's own two leading theories (pool exhaustion, rate-limiter starvation) were checked and ruled out directly against the running container. Actual cause: a silently-swallowed `PermissionError` — a Docker named volume mounts root-owned after the image's build-time `chown`, so the crawler couldn't write to it — fixed with a root-starts/`gosu`-drops entrypoint. Left one related gap explicitly unfixed: any *other* uncaught exception would orphan a claimed row the same way. |
| `09-content-handlers-and-asset-discovery.md` | HTML/image/PDF/video handlers | `<title>`/link-count extraction, image/PDF/video handlers (PNG magic bytes, pypdf, ffprobe). Found and fixed a real scope bug while building it: even with asset `src` extraction added, the host-scope filter still applied to every discovered link, so off-host assets never actually got enqueued despite the settled decision that they should — fixed with `DiscoveredLink.is_asset`. Reviewed via a multi-agent QA/backend-developer loop against the uncommitted diff before commit. Produced the original two-run change-detection table — later found to have been captured via a local `uv run` that routed around the compose hang rather than through compose itself; recaptured for real in the delivery session. |
| `10-adversarial-review.md` | adversarial staff-eng review | A review explicitly framed to find reasons to reject the codebase, not defend it. Ranked five real issues: `contents.content_type NOT NULL` crashing on a response with no `Content-Type` header; no exception boundary around a claimed url, so one bad response could silently kill a worker forever; `Location`/`resolved_url` captured and never read, despite the code's own docstring claiming redirects were followed; a lease-token race window at the claim itself; `encode_body`/`decode_body` as a single-implementation abstraction. One finding (the claim/resume race) was rejected as unfixable by design — the lease bound is what recovers that window — with only its inaccurate docstring corrected. Also produced the diagnosis behind the later fixture-coverage work: every fixture is a response the code already handles well, and every real gap sits on the far side of an `except` clause nothing has ever driven. |
| `11-review-fixes-and-delivery-path.md` | implementing the review findings; the drain-detection fix | Implemented the accepted findings from `10`. The user reported a second, independently-observed compose hang with a confident diagnosis; the assistant reproduced it before writing the requested fix, found the crawl actually self-terminated correctly, and traced the appearance of a hang to a 60-second poll interval an earlier kill happened to land nine seconds before — the reported finding didn't exist, and the user conceded. The real gap that remained (no log line explaining why or how a crawl stopped) got fixed, alongside a genuine, separate improvement: drain detection moved off `lease_recovery_interval_seconds` onto its own fast timer, cutting compose exit time from 69s to 12s. |
