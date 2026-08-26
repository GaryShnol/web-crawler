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

Feature 0 done, no application code yet. Signatures and the schema get written
in here as steps land.

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
to write runs past ~120 lines, stop and tell me what you'd cut. No TODOs, no
stub functions, and nothing in the test suite is allowed to actually sleep —
inject the clock.

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
