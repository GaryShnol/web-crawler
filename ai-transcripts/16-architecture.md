# 16 — Architecture documentation

## Goal

Produce onboarding documentation so a developer who has never seen this repo
understands the mental model in about 5 minutes and knows which file to open
first. Scope is documentation only — no source code changed. Done means:
`README.md` carries a one-glance architecture summary linking out, and
`docs/architecture.md` carries the full onboarding path (mental model, file
map, one sequence diagram, reading order, gotchas, and a reference-only
component map), all claims verified against actual source, not against
CLAUDE.md's prose.

## Delivered

- `README.md` — added a `## Architecture` section (between the project
  description and `## Running it`): one sentence, the 5-box mermaid diagram,
  a link to `docs/architecture.md`. Nothing else was added to this file.
- `docs/architecture.md` — new file. Section structure:
  1. The mental model — 1 sentence + 5-box `flowchart LR` diagram (Frontier →
     Worker → Fetch API → Handler → Storage, with Handler → Frontier labeled
     "new links") + 4 sentences of prose.
  2. Where the code lives — table, one row per diagram box, real file path.
  3. Life of a single URL — happy-path-only `sequenceDiagram` (claim → fetch
     → sniff → store → enqueue), plus a step→file table.
  4. Reading order — 5 files, in call-graph order, one line each on why.
  5. Things that will surprise you — 7 prose paragraphs, no diagram.
  6. Full component map — reference-only: a 15-box `flowchart TD` grouped
     into 6 subgraphs (orchestration/frontier/fetching/parsing/storage/
     observability), the full component table, an external-dependencies
     table, and a list of confirmed-absent infrastructure (no broker, no
     cache, no cron, no headless browser).

## Decisions and why

- README gets only the 5-box diagram, not the full component map. The
  README is the first thing anyone opens; a 15-box diagram there would
  compete with, not support, the one-cycle mental model. The full map is one
  click away.
- The detailed component diagram is demoted to section 6, labeled reference
  material. Sections 1-5 are the actual onboarding path and are meant to be
  read in order; section 6 exists for later lookup ("which file has X"), not
  as required reading. Putting it first would restore the exact
  completeness-over-clarity problem this rewrite was commissioned to fix.
- Pure-function modules (`retry.py`, `errors.py`, `models.py`, `url_tools.py`,
  `config.py`, `logging.py`, `asyncio_util.py`) appear only in the section 6
  table, not as their own diagram boxes anywhere. None of them do I/O or
  hold state; drawing them as boxes with arrows would imply they participate
  in the runtime data flow the diagrams are meant to show, which they don't
  — they're called, not talked to.
- No `classDef`/manual colors anywhere. GitHub renders Mermaid in both light
  and dark mode with no control over which; hardcoded fill/stroke colors can
  make text unreadable in one mode. Node shape is the only emphasis
  mechanism used, and section 6 avoids shaped nodes too after finding a real
  parse risk (see Open items).
- The sequence diagram (section 3) shows only the happy path — claim, fetch,
  sniff, store, enqueue. Retries, lease expiry, and failure branches are
  real and common but are documented as prose in section 5 instead. A
  diagram trying to hold all of that would stop being memorizable, which
  defeats the point of a 5-minute mental model.

## Open items

- Mermaid rendering was never verified by an actual renderer. `npx
  @mermaid-js/mermaid-cli` (`mmdc`) timed out after 3 minutes with no output
  — this sandbox has no Chromium and possibly no network for puppeteer to
  use. All three diagrams were instead checked by hand against GitHub's
  Mermaid constraints (quoting, no `graph` keyword, no click/init/color
  directives, short alphanumeric IDs). Next action: paste each `mermaid`
  block from `README.md` and `docs/architecture.md` into mermaid.live, or
  push this branch and view the rendered files on GitHub directly.
- No other outstanding work from this task.

## Do not redo

- Step 1's full component inventory (entry points, every module, every
  external dependency) is done and is what section 6 of
  `docs/architecture.md` is built from. Every file:line citation in that
  table was read from source directly in this session, not inferred from
  CLAUDE.md. Trust it as-is; don't re-scan the repo to rebuild it.
- The cylinder-shape parse risk in section 6's diagram (`id[("label with
  (parens) inside")]`) was found and fixed by switching those nodes to plain
  rectangles. Don't reintroduce shaped nodes for the external-system boxes
  (Postgres, Fetch API, ffprobe) without re-checking that fix.

## Assumptions

- The real fetch service's wire encoding for `body` is assumed to be
  base64 — `fetch/client.py` documents this as an unverifiable guess, not a
  confirmed fact about the real API.
- Whether anything outside this repo consumes the JSON stdout logs is
  unknown; no aggregator is configured in this codebase, but an external
  log collector can't be ruled out.
- Section 3's sequence diagram collapses "handler registry lookup" and
  "handler.handle()" into one participant (`H`, labeled Handler) and omits
  `record_attempt()` (a diagnostic-only write with no bearing on the
  claim/fetch/sniff/store/enqueue narrative). Both are deliberate
  simplifications for the happy path, not errors — the real call sequence in
  `worker.py:process_one` has these two extra details.
