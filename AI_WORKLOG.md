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

Before delivery, a separate session with none of the build context was pointed
at the repo and asked to find reasons to reject it rather than defend it
(`10-adversarial-review.md`).

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

8. **A code change for the window between a committed claim and the worker
   resuming.** Raised in review as a lost lease. Every pairing of a database
   write with in-process state has that window and `SIGKILL` has it regardless;
   the lease is what bounds it. What was actually wrong was an `engine.py`
   docstring promising an unconditional release. The docstring changed, the
   code didn't.

9. **`error_message` assembled in the persist layer.** Switching on
   `error_kind` in `worker.py` to rebuild a string the classifier already held
   adds one special case per kind. An optional `detail` on `Classification`,
   filled only where the numbers or the caught exception live, leaves all three
   `mark_failed` call sites identical.

10. **A static check enforcing "every `enqueue_many` runs inside the persist
    transaction."** That invariant is what makes the drain check race-free
    without a lock, so it is worth stating — as a sentence where someone would
    break it, not as lint machinery guarding one call site.

## What the tests didn't catch

Three defects reached the end of the build with the suite green.

`docker-entrypoint.sh` was checked out with CRLF, so Linux looked for an
interpreter named `/bin/sh\r`; the container exited 255 before any Python ran.
Drain detection rode lease recovery's 60s interval, so a crawl that finished
nine urls in two seconds took another sixty to exit — correct, and
indistinguishable from a hang to anyone watching. Both surfaced the first time
`docker compose up` was treated as something to run rather than something to
claim.

The third was structural. `site.build_routes()`'s fixture graph had existed
since feature 1, referenced by a link-resolution smoke test and by handler unit
tests calling into it directly — but every end-to-end test in `test_engine.py`
built its own single-url route dict rather than crawling the graph. A response
class that has to be *reachable from the seed page* to matter therefore had no
test that would ever drive it. `test_full_fixture_graph_crawls_clean` is the
first crawl over the whole graph, and it caught `contents.content_type NOT NULL`
on a header-less body that `sniff` matched anyway — a path no unit test calling
`insert_content` directly could reach. The remaining gaps (a redirect, the 4xx
and 5xx routes, a malformed envelope) land the same way: a route, a link from
the seed page, an assertion in that test.

A different kind of miss surfaced in the closing pass over the delivered
docs: two decisions written down early — a per-type `index.jsonl`, SVG
dimensions stored null with a reason — were never built, and stayed
documented as fact in `CLAUDE.md`, then inherited forward into `DESIGN.md`
and `README.md` without either ever being checked against the code. Neither
regressed; both were true the day they were written and false ever after.
Catching that is what the closing pass is for.

## Where I was wrong

I challenged the claim that `INSERT ... ON CONFLICT DO NOTHING` raises a
serialization failure under `REPEATABLE READ`, citing a documentation paraphrase
saying such an insert simply does not proceed. The position was held rather than
conceded, the interleaving re-run with exception introspection, and it returned
SQLSTATE `40001` deterministically. I took the correction.

I also reported the crawler hanging once the frontier drained, and called it the
most severe finding open: nothing set `stop_claiming` on an empty frontier.
Reproduction disproved it. `docker compose up --abort-on-container-exit`
self-terminated at exit 0, and `_supervise` had been setting the event all
along. The real defect sat next to the one I claimed: drain detection rode lease
recovery's 60s interval, so a crawl that finished in two seconds took
sixty-nine to exit. The fix was a second interval, not the mechanism I said was
missing.

I read the fetch API's closed statusCode set (`200|404|429|403|500`, no 3xx) as
proof a redirect could only ever surface as a `200` the API had already followed
on my behalf — the same move `CLAUDE.md` already made for `ETag` against a
status set with no `304` in it, so I extended it to `Location` too, and built
`FetchResult.resolved_url` to catch the header off any response on that premise.
Nothing ever demonstrated a real API 3xx to justify the jump. A later review
challenged the reading using the same evidence the first pass had: the
assignment's table was never a promise that 3xx can't arrive, only that it isn't
documented, and an off-contract status still needs a real classification rather
than being retried to exhaustion as `UNEXPECTED_STATUS`. That reading won —
`resolved_url` came out (one write site, no reader, the same shape
`encode_body`/`decode_body` turned out to be), and `errors.py` gained a
dedicated `PERMANENT_FAILURE`/`REDIRECT` branch instead.

## Transcripts

Every session is in `ai-transcripts/`, one file per step, unedited.
