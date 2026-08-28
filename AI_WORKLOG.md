# AI Work Log

## Platform

Visual Studio Code on Windows, with Claude Code running in the terminal. No
other assistant and no other IDE at any stage. The banner at the top of every
transcript records the version: `v2.1.245` for the two design sessions,
`v2.1.246` through the build, `v2.1.247` for review and delivery.

## Models, by stage

| Stage | Sessions | Model |
|---|---|---|
| Planning / design | `01-decisions`, `1.2-decisions` | Claude Sonnet 5 |
| Scaffolding | `02-scaffolding` | Claude Sonnet 5 |
| Implementation | `03-errors` through `09-content-handlers-and-asset-discovery` | Claude Sonnet 5 |
| Review / refactor | `10-adversarial-review`, `11-review-fixes-and-delivery-path`, `12-close-leftover` | Claude Sonnet 5 |

The choice was not deliberate. Sonnet 5 is the model my Claude Pro plan gives
me, so there was nothing to choose between at any stage. With a second model
available I'd have put planning and the adversarial review on a reasoning
model — arguing against my own design is the whole point of those two — and
kept Sonnet for the implementation steps, which are bounded and
contract-first.

## How I directed it

One session per step. Each is seeded from `CLAUDE.md`, is made to ask design
questions before writing code, and ends with a `CONTRACT-UPDATE` block I paste
back into `CLAUDE.md`. That shape is why the transcripts read as arguments
rather than briefs.

Before delivery I opened a session with none of the build context, pointed it
at the repo, and asked it to find reasons to reject the code rather than
defend it (`10-adversarial-review.md`).

Everything below is mine to defend. What I rejected, where I was wrong, and
what neither of us caught until something actually ran.

## What I rejected

1. **`enqueue_many` as a single `WITH` query.** Under Read Committed both
   sub-statements share the snapshot taken at statement start, so a URL
   another transaction is committing at that instant appears in neither the
   `INSERT`'s `RETURNING` nor the sibling `SELECT` — no id comes back and its
   `links` edge is dropped. I reproduced it against a live Postgres, then split
   it into two statements so the read takes its own snapshot. (`05-frontier`)

2. **Expired leases reclaimed inside `claim_batch`'s `WHERE`.** At 50k `done`
   and 2k `pending`, the `OR` forces a `BitmapOr` and a `Sort` over all 2000
   eligible rows before `LIMIT`: 66 buffers. A `pending`-only ordered index
   scan stops at the limit: 3. Recovery went back to its own statement.

3. **Terminal writes matched on `id` alone.** `claim_batch` returned no owner,
   so a worker whose lease had expired and been reclaimed mid-fetch would still
   write `done` over the row's new owner — "processed at most once" broken by
   the recovery path that exists to preserve it. `claim_batch` now mints a
   `lease_token` per claim, and every write that could land on a row the caller
   no longer owns checks it; zero rows matched is a lost race, logged, not
   raised. (`07-crawl-engine-content`)

4. **Structured log context flattened into an f-string.** `_ContextFilter`
   overwrote `record.context` instead of merging it, so a call site's own
   `extra={"context": ...}` never reached stdout. The fix I was offered
   interpolated those fields into the message text, which makes them
   unqueryable. The filter merges now; the call sites stayed structured.

5. **`Pool | Connection` as a parameter type.** Both satisfy `.execute`, which
   is why it typechecks and why it's the wrong type: it hides whether a write
   is the claim's own short transaction or part of the caller's longer one.
   `store/frontier.py` takes a `Connection`; the pool stays in `engine.py` and
   `cli.py`.

6. **`fetch_attempts` proposed as dead weight.** `urls.attempts` is hot state
   `claim_batch` bumps in the same statement as the lease; per-attempt
   `status_code` and `duration_ms` are a different axis a counter can't hold.
   Both stayed.

7. **`max_attempts` inside the frontier.** The give-up decision belongs to
   `fetch/retry.py`; `mark_failed` takes it as an argument and writes it.

8. **A code change for the window between a committed claim and the worker
   resuming.** Raised in review as a lost lease. Every pairing of a database
   write with in-process state has that window, and `SIGKILL` has it
   regardless; the lease bound is what recovers it. What was actually wrong was
   an `engine.py` docstring promising an unconditional release. The docstring
   changed, the code didn't. (`10-adversarial-review`)

9. **`error_message` assembled in the persist layer.** Switching on
   `error_kind` in `worker.py` to rebuild a string the classifier already held
   adds a special case per kind. An optional `detail` on `Classification`,
   filled only where the numbers or the caught exception live, leaves all three
   `mark_failed` call sites identical.

10. **A static check enforcing "every `enqueue_many` runs inside the persist
    transaction."** That invariant is what makes drain detection race-free
    without a lock, so it's worth stating — as a sentence where someone would
    break it, not as lint machinery guarding a single call site.

## Where I was wrong

I argued that `INSERT ... ON CONFLICT DO NOTHING` doesn't raise a
serialization failure under `REPEATABLE READ`, citing a documentation
paraphrase saying such an insert simply does not proceed. I held the position
rather than conceding it, re-ran the interleaving with exception
introspection, and got SQLSTATE `40001` deterministically. I was wrong, and
asked for the correct mechanism to be written into `DESIGN.md`.

I reported the crawler hanging once the frontier drained and called it the
most severe finding open: nothing set `stop_claiming` on an empty frontier.
Reproduction disproved it. `docker compose up --abort-on-container-exit`
self-terminated at exit 0 and `_supervise` had been setting the event all
along; an earlier kill had simply landed nine seconds into a 60-second poll.
The real defect sat next to the one I claimed — drain detection rode
`lease_recovery_interval_seconds`, so a crawl that finished in two seconds
took 69s to exit. Moving it onto its own fast timer cut that to 12s. The fix
was a second interval, not the mechanism I said was missing.

I read the fetch API's closed status set (`200|404|429|403|500`, no 3xx) as
proof that a redirect could only ever surface as a `200` the API had already
followed — the same move `CLAUDE.md` made for `ETag` against a status set with
no `304` in it — and built `FetchResult.resolved_url` on that premise. Nothing
ever demonstrated a real API 3xx to justify the jump. A later review
challenged the reading with the same evidence I'd had: the assignment's table
says 3xx isn't documented, not that it can't arrive, and an off-contract
status needs a real classification rather than being retried to exhaustion as
`UNEXPECTED_STATUS`. That reading won. `resolved_url` came out — one write
site, no reader — and `errors.py` gained a `PERMANENT_FAILURE`/`REDIRECT`
branch.

## What the tests didn't catch

Three defects reached the end of the build with the suite green.

`docker-entrypoint.sh` was checked out with CRLF, so Linux looked for an
interpreter named `/bin/sh\r` and the container exited 255 before any Python
ran. The 60-second drain delay above was the second. Both surfaced the first
time I ran `docker compose up` instead of claiming it worked.

The third was structural. `site.build_routes()`'s fixture graph had existed
since feature 1, referenced by a link-resolution smoke test and by handler
unit tests calling into it directly — but every end-to-end test in
`test_engine.py` built its own single-url route dict instead of crawling the
graph. A response class that only matters if it's *reachable from the seed
page* therefore had no test that could ever drive it. The test that closed the
gap is the first crawl over the whole graph, and it caught
`contents.content_type NOT NULL` on a header-less body that `sniff` matched
anyway — a path no unit test calling `insert_content` directly could reach.
The remaining gaps (a redirect, the 4xx and 5xx routes, a malformed envelope)
land the same way: a route, a link from the seed page, an assertion in that
test.

A fourth turned up in the closing pass over the delivered docs. Two decisions
written down early — a per-type `index.jsonl`, SVG dimensions stored null with
a reason — were never built, but stayed in `CLAUDE.md` as fact and were
inherited forward into `DESIGN.md` and `README.md` without either being
checked against the code. Nothing regressed; the docs simply described a
system that didn't exist. That pass is what it's for.

## Transcripts

`ai-transcripts/`, one file per step, unedited, with
`ai-transcripts/README.md` indexing what each session decided and where the
argument moved the design.
