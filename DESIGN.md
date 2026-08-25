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
