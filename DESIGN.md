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
