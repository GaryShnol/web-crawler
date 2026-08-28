# Web Crawler

Given a seed URL, discovers and downloads a site's HTML, images, video and
PDFs through the fetch API, staying within the seed's host, surviving fetch
failures and a crash mid-crawl, and resuming where it left off.

## Running it

```
docker compose up --abort-on-container-exit
docker compose run --rm crawler stats
```

Brings up Postgres, a fake fetch API and the crawler, which exits once the
frontier drains. `tests/fake_api/` is a test double for offline development,
not an implementation of the fetch service the assignment specifies: pointing
the crawler at a different gateway is `FETCH_API_URL` plus a seed argument, no
code change.

## Key decisions

**Frontier: Postgres, not a queue.** `FOR UPDATE SKIP LOCKED` claims a url
atomically, and a `lease_token` fences every terminal write against the claim
that made it, so a worker whose lease was reclaimed can't overwrite whoever
holds the row next. Redis was rejected: a lease split still has to re-check
Postgres before re-fetching, so it adds a hop it can't remove.

**Rate limiting: one shared AIMD bucket** keyed to the gateway, not one per
host — the crawler only ever talks to the gateway, so every 429 is a signal
about it, not about any origin behind it.

**Handlers: a registry keyed on content type sniffed from the body's own magic
bytes**, never the declared header or the URL extension. A fifth type is a new
file, its `@register`, and one import line.

**Failures: a closed set of outcomes, classified once.** 404/403 permanent,
429/500 temporary with jittered backoff. A 3xx — absent from the assignment's
own status table — is permanent too: a deterministic answer about one url, so
retrying it meets the identical redirect every time.

**Storage: sha256-prefixed filenames, one tree per type.** The HTML tree is
`output/pages/` rather than the brief's `output/html/` — organization is left
to the implementer, and `pages` names what the directory holds rather than the
format it's in.

## Trade-offs

**No queue, no cache.** Postgres alone holds frontier and dedup state; a queue
beside it means two writes for one fact, and a crash between them either loses
a url or repeats it.

**Two spellings of one URL stay two URLs.** Query parameters aren't reordered —
sorting could silently merge urls serving different content. The cost is one
redundant fetch, which content-hash dedup collapses to one stored file.

**The wire format is a guess.** `Buffer` has no declared JSON encoding, so the
base64 assumption lives at two call sites — `client.py`'s decode,
`tests/fake_api`'s encode. If the real API differs, those two change and
nothing downstream does.

## At production scale

`claim_batch`'s `pending`-only index scan keeps a claim cheap as `done` rows
reach the millions — confirmed with `EXPLAIN (ANALYZE, BUFFERS)` at 50k/2k
rows, not load-tested past that. A flat directory per type stops being enough
past a few million objects; `urls.content_hash` already maps a file back to the
urls that produced it, so blobs move to S3 with Postgres keeping the pointer.
Cost is set by the gateway's rate limit, not by compute: the limiter paces to
whatever the gateway grants, so workers past that ceiling only queue faster.

## What I'd do differently

**Lease renewal.** Nothing lets a live worker prove it's still working, so
`lease_seconds` can't tell a slow worker from a dead one and a legitimately slow
fetch gets reclaimed. Fencing keeps the outcome correct, but the fetch runs
twice. Found empirically: the SIGKILL resumability test needed a lease six times
longer than crash recovery alone would justify.

**`<base href>` and a second raster format** — one function, and a file plus its
`@register`. Deferred because the fixture didn't need them.

Full decision log and measurements: `DESIGN.md`. AI development record:
`AI_WORKLOG.md` and `ai-transcripts/`.
