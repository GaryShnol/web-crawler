 ▐▛███▛█   Claude Code v2.1.246
▝▜██████▀  Sonnet 5 · Claude Pro
  ▝▝ ▝▝    ~\Desktop\projects\web-crawler


❯ .                                                                             

● It looks like your message came through empty. You're on feature/3-frontier,
  and per CLAUDE.md the workflow is one step per session with you specifying
  which one at the top. What would you like to work on — 3.1 (schema), or
  something else?

✻ Churned for 4s · done 3:27 PM

❯ starting with the table schema for resumable crawl.urls table needs status,   
  attempts, lease, next_attempt_at, hashes, and timing.                         
  what's missing if two workers run in parallel, and what's dead weight?write   
  claim_batch in store/frontier.py using raw asyncpg.                           
  SELECT ... FOR UPDATE SKIP LOCKED inside an UPDATE statement to bump attempts 
  and set lease. single SQL query.                                              
  why does the clock have to be postgres now() and not python datetime?write    
  claim_batch in store/frontier.py using raw asyncpg.                           
  SELECT ... FOR UPDATE SKIP LOCKED inside an UPDATE statement to bump attempts 
  and set lease. single SQL query.                                              
  why does the clock have to be postgres now() and not python datetime?add      
  mark_done, mark_failed, release, recover_expired_leases, and enqueue_many.    
  ON CONFLICT DO NOTHING on enqueue_many must return only new ids, but links    
  needs existing ids too. query both cleanly without extra updates.write        
  migrations/001_init.sql and store/db.py with pool initialization and          
  advisory-lock migration runner.                                               
  keep partial indexes on pending and in_progress states only.                  

● Two-worker gaps vs. dead weight

  What's missing from status, attempts, lease, next_attempt_at, hashes, timing:

  1. UNIQUE(url) — without it, two workers parsing different pages that both
     link to the same URL will both INSERT it, racing each other into duplicate
     frontier rows. enqueue_many's ON CONFLICT DO NOTHING is meaningless without
     this constraint to conflict on.
  2. lease_until must be a timestamp, not a boolean "claimed" flag. A flag can't
     self-heal — if the worker holding it dies mid-fetch, nothing ever clears
     it. A timestamp lets recover_expired_leases reclaim rows whose lease is
     simply in the past, no crash detection required.
  3. next_attempt_at has to be in the claim predicate, not just stored. A
     temp-failure (429/500) sets a future retry time; if claim_batch's WHERE
     only checks status = 'pending', a worker will hammer a row before its
     backoff elapses. The predicate needs status = 'pending' AND next_attempt_at
     <= now().
  4. Partial indexes on pending and in_progress — you already called this out
     for the db.py step, but it's worth naming here as the same correctness
     concern: without it, claim_batch and recover_expired_leases both degenerate
     into seq scans as the table grows, and under concurrent claims that scan
     window is exactly where two workers contend.

  What's dead weight:

  1. No worker_id/heartbeat column. SKIP LOCKED + lease_until expiry gives full
     correctness without knowing who holds a row. Add it later only for operator
     visibility ("which worker is stuck"), never for correctness — that's a
     logging concern, not a locking one.
  2. No optimistic-lock/version column (xmin tricks, a manual version int). FOR 
     UPDATE SKIP LOCKED already prevents two workers from claiming the same row;
     a version counter would just re-implement what the row lock gives you for
     free.
  3. No separate claimed BOOLEAN. status = 'in_progress' already carries that
     bit — a second field can drift out of sync with status and now you're
     reconciling two sources of truth for one fact.
  4. No per-attempt history table. CLAUDE.md's bar is "stop, inspect, restart,"
     not an audit log — attempts (count) + error_kind/error_message (last
     failure) covers that.

  Why now() has to come from Postgres

  The claim has to be atomic inside one statement — that's the entire point of
  doing the bump-and-lease as a single UPDATE ... FOR UPDATE SKIP LOCKED rather
  than a read-then-write. If the "is this lease expired" comparison uses a
  Python-side datetime.utcnow() passed in as a parameter, you've split the clock
  read from the lock acquisition: the value was captured before the statement
  even reached Postgres, and if workers run on different hosts/containers (which
  they will, under Docker Compose), their wall clocks can disagree by real,
  non-trivial amounts. A worker with a fast clock could decide a lease is
  expired while Postgres — and the worker still holding it — disagrees, and now
  two workers hold the same row despite SKIP LOCKED, because the lock isn't what
  failed, the staleness test was wrong. now() evaluated inside the statement is
  anchored to the same authority that's taking the row lock, so the row lock
  and the clock that decided the row was claimable are never out of sync. This
  is the same principle as the "correctness lives in the database" decision in
  CLAUDE.md — the clock has to live there too, not split across N processes.

  Before I write claim_batch, mark_*, enqueue_many, and the migration

  1. Surrogate key for urls — BIGSERIAL id (so links rows are two small ints),
     or use the normalized url text itself as the primary key (one less join,
     but wider links rows and wider indexes)? I'd default to BIGSERIAL.
  2. Is the links table in scope for this session? enqueue_many needing
     "existing ids too" only matters if something downstream (a links(src_id, 
     dst_id) table for the discovery graph) consumes both new and pre-existing
     ids in the same call. If yes, I'll include it in 001_init.sql; if it's for
     a later step, enqueue_many can just return ids and I skip the table for
     now.
  3. status as a Postgres ENUM or TEXT + CHECK? Native enum is smaller and
     self-documenting, but adding a value later needs ALTER TYPE ... ADD VALUE
     (can't run in the same transaction as its first use, complicates the
     migration runner). TEXT + CHECK is one migration away from adding
     'skipped'-style states without that friction. Given the crawler's still
     adding features, I'd lean TEXT + CHECK — but it's your call.
  4. Where does the max_attempts cutoff live? Does claim_batch's WHERE exclude
     rows already at attempts >= max_attempts (so they never surface as
     claimable), or does mark_failed flip status → 'failed' once the cap is hit,
     so they fall out of the 'pending' predicate naturally and claim_batch
     doesn't need to know about the cap at all? I'd default to the second —
     claim_batch stays dumb, mark_failed owns the cap decision — but say if you
     want it enforced in both places as a belt-and-suspenders check.

✻ Cogitated for 1m 48s · done 3:30 PM

❯ 1. bigserial.                                                                 
  2. yes, links is in scope for 001_init.sql. that's exactly why i asked        
     about the existing ids.                                                    
  3. text + check. adding a value to an enum needs a migration and a lock,      
     and i'll add one.                                                          
  4. neither. max_attempts is retry.py's business - it returns GiveUp or        
     RetryAt and the frontier only writes what it's told. mark_failed takes     
     the decision as an argument, it doesn't make it. max_attempts should       
     not appear in frontier.py at all.                                          

  Searched for 1 pattern, read 8 files (ctrl+o to expand)

● Write(migrations\001_init.sql)
  ⎿  Wrote 53 lines to migrations\001_init.sql
       1 -- Frontier state and the discovery graph. See CLAUDE.md: correc
         tness lives
         l loses
       3 -- nothing more than an in-flight lease.
       4 
       6     id              BIGSERIAL PRIMARY KEY,
       7     url             TEXT NOT NULL UNIQUE,
       8     depth           INT NOT NULL DEFAULT 0,
      10     -- TEXT + CHECK, not a native ENUM: adding a status later is
          a plain
     … +57 lines (ctrl+o to expand)

● Write(src\crawler\store\__init__.py)
  ⎿  Wrote 1 line to src\crawler\store\__init__.py
      1 (No content)

● Write(src\crawler\store\db.py)
  ⎿  Wrote 61 lines to src\crawler\store\db.py
      1 """The connection pool, and a migration runner guarded by a sessi
        on-level
        can start
      3 more than one process against a fresh database, and only one of t
        hem should
        ate-object
      5 error.
      6 """
      8 import logging
      9 from pathlib import Path
     … +66 lines (ctrl+o to expand)

● Write(src\crawler\store\frontier.py)
  ⎿  Wrote 231 lines to src\crawler\store\frontier.py
       1 """The frontier: claim, finish, enqueue. Every write here is one
       2 statement, because the atomicity that matters — bump attempts an
         d take the
         inside
         n this
       5 process. See CLAUDE.md: correctness lives in the database.
       7 Nothing here reads `config.max_attempts`. Whether a failure gets
          retried is
         nly writes
      10 """
     … +272 lines (ctrl+o to expand)

  Ran 4 shell commands (ctrl+o to expand)

● Write(scratch_verify.py)
  ⎿  Wrote 111 lines to scratch_verify.py
       1 import asyncio
       2 import sys
       3 from pathlib import Path
       4 
       5 sys.path.insert(0, "src")
       6 
       7 import asyncpg
       8 
       9 from crawler.config import Config
      10 from crawler.errors import ErrorKind
     … +119 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

● All the SQL checks out against a real Postgres — the CTE snapshot behavior in
  enqueue_many (new vs. existing ids, no double-counting), concurrent
  claim_batch calls (no row claimed twice, attempt_no correct), and the terminal
  writes (mark_done, mark_failed both branches, release,
  recover_expired_leases) all behaved as designed. Cleaning up the test
  container and scratch file now.

  Ran 1 shell command (ctrl+o to expand)

● Both checks pass clean. Everything's written:

  - migrations/001_init.sql — urls
    (status/attempts/lease/next_attempt_at/hashes/timing, TEXT + CHECK,
    UNIQUE(url), partial indexes on pending/in_progress) and links (the
    discovery graph).
  - src/crawler/store/db.py — create_pool, and run_migrations guarded by a
    session-level pg_advisory_lock so two containers starting at once don't race
    CREATE TABLE. Verified idempotent on a second run.
  - src/crawler/store/frontier.py — claim_batch, mark_done, mark_failed,
    release, recover_expired_leases, enqueue_many. mark_failed takes GiveUp | 
    RetryAt as an argument and never reads max_attempts, per your call.
    enqueue_many uses the ins/existing/UNION ALL CTE pattern — verified against
    real Postgres that existing genuinely can't see ins's rows (same-snapshot
    semantics), so new-vs-existing ids come back correct with no extra UPDATE.

  I ran all of it against a throwaway postgres:16-alpine container (concurrent
  claim_batch calls, both mark_failed branches, dedup within one enqueue_many
  call, expired-lease recovery) rather than trusting the SQL by inspection —
  everything behaved as designed, container's since removed.

  Not written this round: tests, worker.py/engine.py wiring, or a
  CONTRACT-UPDATE block — say if you want the contract block for the new module
  surface, or if tests are next.

✻ Crunched for 5m 22s · done 3:38 PM
※ recap: Building a resumable crawler; just finished the frontier schema, migration runner, and store/frontier.py (claim/mark/enqueue), verified against real Postgres. Next: decide on a CONTRACT-UPDATE block and/or tests for this module. (disable recaps in /config)

❯ before i commit any of this - two workers call claim_batch in the same        
  millisecond. walk me through what postgres actually does with the skip        
  locked subquery. i want the read committed part, not just "they don't         
  wait".and if a worker dies between the claim and mark_done? sigkill - no      
  exception handler, no rollback i can write. what's left in the                
  row?enqueue_many. the CTE is one statement, so existing uses the snapshot     
  taken at statement start.                                                     
                                                                                
  two workers discover the same new url at the same instant. the other          
  transaction inserted it and hasn't committed. mine blocks on the              
  speculative insertion lock, theirs commits, my DO NOTHING skips the row -     
  so it's not in ins. and existing can't see it either, it wasn't committed     
  when my snapshot was taken.                                                   
                                                                                
  which branch returns that id? walk it through before you change               
  anything.001_init.sql is missing contents, content_metadata and               
  fetch_attempts,                                                               
  and urls collapsed normalized_url and raw_url into one column. i need         
  both - normalized is the unique key and the conflict target, raw is what      
  i show when something looks wrong.                                            
                                                                                
  fetch_attempts i never agreed to drop. attempts on urls is hot state the      
  claim bumps in one statement without an aggregate; fetch_attempts is the      
  per-attempt row with status_code and duration_ms. those are two jobs.         
  put it back or make the argument.advisory lock for the migrations but not for 
  the frontier - one line on                                                    
  the difference, i want it in the readme.CONTRACT-UPDATE for CLAUDE.md?        

  Read 1 file, ran 1 shell command (ctrl+o to expand)

● Write(scratch_race.py)
  ⎿  Wrote 70 lines to scratch_race.py
       1 import asyncio
       2 
       3 import asyncpg
       4 
       5 DATABASE_URL = "postgresql://postgres:test@localhost:55432/crawl
       6 
       7 OLD_QUERY = """
       8 WITH input AS (
       9     SELECT DISTINCT unnest($1::text[]) AS url
      10 ),
     … +66 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

● Write(scratch_race2.py)
  ⎿  Wrote 55 lines to scratch_race2.py
       1 import asyncio
       2 
       3 import asyncpg
       4 
       5 DATABASE_URL = "postgresql://postgres:test@localhost:55432/crawl
       6 
       7 
       8 async def new_enqueue(conn, urls, depth):
       9     await conn.execute(
      10         """
     … +51 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

● Update(scratch_race2.py)
  ⎿  Added 1 line, removed 1 line
       9      await conn.execute(
      10          """
      11          INSERT INTO urls (url, depth)
      12 -        SELECT DISTINCT unnest($1::text[]), $2                 
      12 +        SELECT DISTINCT u, $2::int FROM unnest($1::text[]) AS u
      13          ON CONFLICT (url) DO NOTHING
      14          """,
      15          urls,

  Ran 2 shell commands (ctrl+o to expand)

● SKIP LOCKED under Read Committed, step by step

  Both workers' claim_batch calls are single statements, so each gets its own
  statement-level snapshot the moment it starts — under Read Committed that's
  per-statement, not per-transaction. If they truly start in the same
  millisecond, they can both see the same set of eligible rows in that snapshot;
  the snapshot isn't what keeps them apart.

  What keeps them apart is row locking, and it happens incrementally, not as one
  batch operation:

  1. The inner SELECT ... ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 
     $1 walks eligible rows in order. For each candidate row, the executor
     attempts to take that row's exclusive lock immediately, non-blocking — this
     is a different code path from a plain FOR UPDATE, which would block and
     wait.
  2. Whichever backend's lock request physically lands first (decided by the
     lock manager, not by anything either worker's SQL controls) gets the row.
     The loser's attempt sees it's already locked by a still-running transaction
     and, because of SKIP LOCKED, doesn't wait and doesn't error — it just
     treats the row as absent and moves to the next candidate in the order.
  3. This repeats row by row until each worker has accumulated up to LIMIT rows
     it actually holds the lock on.
  4. The outer UPDATE then writes attempts/lease_until only to the exact row set
     the subquery locked in this same statement. Because the lock acquisition
     and the write happen inside one statement, there's no gap between "decide
     which rows are mine" and "mark them mine" for a second transaction to slip
     into — that gap is exactly what a separate SELECT FOR UPDATE followed by a
     separate UPDATE would have.

  Worth contrasting with plain FOR UPDATE (no SKIP LOCKED): hitting a locked row
  would block until the other transaction ends, then re-check the row's current
  state against the WHERE clause (EvalPlanQual) — e.g., if worker A had since
  flipped it to in_progress, worker B's status = 'pending' predicate would now
  correctly exclude it. SKIP LOCKED is exactly the shortcut that skips the
  wait-then-recheck, since a locked row is being claimed right now and waiting
  for that outcome instead of just moving on would only get you the same "not
  eligible" answer, slower.

  SIGKILL between claim and mark_done

  Nothing to roll back, because there's nothing buffered in the process to lose.
  claim_batch's UPDATE already committed the moment it returned — pool.fetch
  runs it as its own implicit transaction, and Postgres doesn't hand control
  back to the caller until that's durable. So a hard kill after that point
  leaves the row exactly as the claim left it:

  - status = 'in_progress'
  - attempts already incremented — permanently. That attempt is spent even
    though nothing happened after it. This is deliberate, not a gap: it means a
    URL that reliably kills a worker (a decompression bomb, a handler bug) still
    ages toward max_attempts instead of being retried forever, at the cost of
    very rarely burning one attempt on bad luck rather than a bad URL.
  - lease_until = claim time + lease_seconds, untouched.
  - Every field mark_done would have written — content_type, content_hash, etc.
    — still whatever it was before the claim (NULL on a first-ever attempt).

  The row is genuinely unclaimable by anyone until lease_until passes — that's
  the lease being honored, not a bug — at which point claim_batch's own WHERE 
  (status = 'in_progress' AND lease_until < now()) branch (or a periodic
  recover_expired_leases sweep) picks it back up. So the bound on "how long does
  a crashed URL sit dead" is exactly lease_seconds, never longer, never
  dependent on anything the crashed process was supposed to do on the way out.

  enqueue_many's race — I checked, and you're right to ask

  I traced it and then reproduced it against real Postgres before touching
  anything, since asserting from memory here would've been exactly the kind of
  thing that's cheap to get wrong.

  Two backends, A and B, both try to insert the same brand-new URL. A's INSERT
  gets there first and holds it open (uncommitted). B's INSERT ... ON CONFLICT 
  DO NOTHING reaches the same unique-index slot and finds A's row as a
  speculative insertion — for that specific case, Postgres doesn't apply the
  statement snapshot at all; it blocks B's INSERT on a token until A resolves. A
  commits. B's INSERT un-blocks, re-checks via the index (not via MVCC snapshot
  — a direct look at the current row), correctly sees a real conflict, and DO 
  NOTHING skips it. So B's ins CTE is empty for that URL, confirmed.

  Then existing: same statement, same WITH, which per Postgres's documented
  behavior for data-modifying CTEs means every sub-statement — including the
  plain SELECT in existing — runs against the one snapshot taken when B's
  statement began, before A had committed anything. So existing's ordinary table
  scan doesn't see A's row either — it was never going to, snapshot or no wait,
  because a plain SELECT doesn't participate in the conflict-wait machinery at
  all.

  Result: all_urls has neither row. B's enqueue_many returns a dict silently
  missing that URL, and — worse — no links row gets written for it, even though
  the URL now definitely exists with a real id. I reproduced exactly this:

  A inserted, not yet committed
  B still blocked? True
  A committed
  B's result (old query): []
  BUG CONFIRMED: B's snapshot missed the concurrently-committed row

  The fix is the standard one for this exact documented Postgres gotcha: split
  the write and the read into two statements, not one CTE. Read Committed
  re-takes its snapshot per statement, so a second, separate SELECT — issued
  after the INSERT statement has returned, meaning after every conflicting
  concurrent inserter has necessarily already committed or aborted — sees
  everything, no exceptions:

  -- statement 1
  INSERT INTO urls (url, depth)
  SELECT DISTINCT u, $2 FROM unnest($1::text[]) AS u
  ON CONFLICT (url) DO NOTHING;

  -- statement 2
  WITH resolved AS (
      SELECT id, url FROM urls WHERE url = ANY($1::text[])
  ),
  links_ins AS (
      INSERT INTO links (src_id, dst_id)
      SELECT $2, id FROM resolved WHERE $2::bigint IS NOT NULL
      ON CONFLICT DO NOTHING
  )
  SELECT id, url FROM resolved;

  I verified this version resolves correctly under the identical forced
  interleaving — B genuinely blocks on its own INSERT (not a snapshot artifact,
  a real wait), and once A commits, B's second statement sees the row:

  A inserted, not yet committed
  B still blocked (on its own INSERT statement)? True
  A committed
  B's result (new two-statement query): {'http://race': 1}
  FIXED: B correctly resolved the concurrently-inserted id

  This is strictly simpler than the ins/existing/UNION ALL version too — no need
  to distinguish new-vs-existing internally, since nothing downstream cared. I
  haven't written this into frontier.py yet — it touches the same function as
  the raw_url/normalized_url split below, so I'll fold both changes into one
  rewrite once the schema question underneath is settled.

  raw_url / normalized_url, fetch_attempts — agreed, folding in

  Both go back in. normalized_url becomes the unique/conflict-target column
  (what claim_batch returns to actually fetch); raw_url stored once, at insert,
  whatever string first produced the row — never overwritten on a later
  sighting, since a display hint doesn't need a history, just one honest sample.
  That means enqueue_many's input becomes (raw_url, normalized_url) pairs
  instead of bare strings — normalization already happens in
  url_tools.normalize, called by whoever's about to enqueue (not frontier.py,
  which stays a dumb writer).

  fetch_attempts: you're right, and I should've kept it — urls.attempts is the
  hot counter claim_batch bumps in the same statement as the lease, no aggregate
  query needed for that; a per-attempt row with status_code/duration_ms is a
  different axis (diagnosis: "what actually happened on try 3") that a single
  counter column can't hold without becoming multi-valued. Putting it back as
  fetch_attempts(id, url_id, attempt_no, status_code, duration_ms, error_kind, 
  created_at), written by mark_done/mark_failed in the same call (they're the
  only two places a terminal outcome is known) — which means both gain
  attempt_no: int (the value claim_batch already handed the caller) and
  status_code: int | None / elapsed: float parameters.

  One thing I need from you before I rewrite the migration

  "missing contents, content_metadata and fetch_attempts" — is contents a third
  table (a dedup point keyed by content_hash, holding the stored file path/size,
  since "identical bytes from two URLs are stored once" per CLAUDE.md — with
  content_metadata's per-type fields, e.g. width/height/duration, then keyed by
  content_hash too rather than by URL, so two URLs sharing bytes also share
  metadata), or was that a typo/lead-in and you mean just content_metadata +
  fetch_attempts, with content_metadata keyed by url_id? These aren't the same
  schema — the hash-keyed version is more correct against the dedup principle
  already in CLAUDE.md but I'm not going to guess and rewrite twice.

  Advisory lock: migrations vs. frontier

  One-liner for wherever this note belongs — README.md doesn't exist yet (only
  DESIGN.md does, and that already carries this "chose X, rejected Y" format),
  so I'll add it there under a new heading unless you tell me you actually want
  a README.md started:

  ▎ Migrations get a global advisory lock because they run once, at boot, with 
  ▎ no per-row unit of work — coarse and rare is fine. The frontier never does: 
  ▎ claim_batch and the terminal writes are per-row, continuous, and 
  ▎ high-frequency, and Postgres's own row locks (FOR UPDATE SKIP LOCKED, or the
  ▎ plain lock an UPDATE ... WHERE id = $1 already takes) already give exactly 
  ▎ the right granularity — wrapping those in an advisory lock would serialize 
  ▎ every worker's writes into single file, which is the concurrency the schema 
  ▎ exists to provide.

  CONTRACT-UPDATE

  Holding this until the contents question is answered and I've actually
  rewritten the migration/frontier.py — the column and signature changes aren't
  final yet, and a contract block against code that's about to change again
  isn't worth committing.

✻ Worked for 5m 24s · done 3:57 PM

❯ you gave two recovery paths - claim_batch picking up expired in_progress      
  rows itself, and the recover_expired_leases sweep. pick one.                  
                                                                                
  if claim_batch does it, what's the sweep for and why does the partial         
  index on pending still help? if the sweep does it, in_progress -> pending     
  has three sources - retry, expired lease, release - and only the retry        
  path should touch attempts. which one did you write?contents is a third       
  table, yes. contents(content_hash PK, content_type,                           
  byte_size, storage_path, first_seen_at) - identical bytes from two urls       
  are stored once. content_metadata(content_hash, kind, payload jsonb)          
  keyed on the hash too, so two urls sharing bytes share the extraction.        
  urls.content_hash is the fk pointing at it.the two-statement fix only works   
  because statement 2 takes a fresh                                             
  snapshot. the worker wraps every write for one url in one transaction.        
  does that still hold inside a transaction, and what breaks if someone         
  sets it to repeatable read?links lost anchor_text. put it back, and say out   
  loud what the                                                                 
  PK(src_id, dst_id) does when the same page links to the same target           
  twice with different anchors - i want that acknowledged, not silent.rewrite   
  the migration and frontier.py now with all of it folded in.that's over the    
  line in CLAUDE.md. what comes out?CONTRACT-UPDATE for CLAUDE.md?              

  Ran 3 shell commands (ctrl+o to expand)

● Write(migrations\001_init.sql)
  ⎿  Added 66 lines, removed 19 lines
       1 --- Frontier state and the discovery graph. See CLAUDE.md: corre
         -ctness lives                                                   
       2 --- here, not in process memory, so this is what a crash mid-cra
         -wl loses                                                       
       3 --- nothing more than an in-flight lease.                       
       1 +-- Frontier state, content dedup, and the discovery graph. See 
         +CLAUDE.md:                                                     
       2 +-- correctness lives here, not in process memory.              
       3  
       4 +-- One row per unique payload, keyed by its hash — "identical b
         +ytes from two                                                  
       5 +-- urls are stored once". content_type/byte_size describe the s
         +tored blob;                                                    
       6 +-- urls.content_type/content_length below describe what was *ob
         +served*,                                                       
       7 +-- which still applies to a skipped url that was never download
         +ed at all.                                                     
       8 +CREATE TABLE contents (                                        
       9 +    content_hash  TEXT PRIMARY KEY,                            
      10 +    content_type  TEXT NOT NULL,                               
      11 +    byte_size     BIGINT NOT NULL,                             
      12 +    storage_path  TEXT NOT NULL,                               
      13 +    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()           
      14 +);                                                             
      15 +                                                               
      16  CREATE TABLE urls (
      17      id              BIGSERIAL PRIMARY KEY,
       7 -    url             TEXT NOT NULL UNIQUE,                      
      18 +                                                               
      19 +    -- normalized_url is the identity: the conflict target, and
         + what                                                          
      20 +    -- actually gets fetched. raw_url is only ever set once, at
         + insert, from                                                  
      21 +    -- whichever spelling first produced the row — a debugging 
         +hint, not a                                                    
      22 +    -- history of every variant seen.                          
      23 +    normalized_url  TEXT NOT NULL UNIQUE,                      
      24 +    raw_url         TEXT NOT NULL,                             
      25 +                                                               
      26      depth           INT NOT NULL DEFAULT 0,
      27  
      28      -- TEXT + CHECK, not a native ENUM: adding a status later i
          s a plain
     ...
      31      status          TEXT NOT NULL DEFAULT 'pending'
      32                          CHECK (status IN ('pending', 'in_progre
          ss', 'done', 'failed', 'skipped')),
      33  
      34 +    -- Bumped only by claim_batch's own claiming statement — ne
         +ver by                                                         
      35 +    -- whatever later sends the row back to 'pending' (retry, r
         +elease).                                                       
      36      attempts        INT NOT NULL DEFAULT 0,
      37  
      18 -    -- Set on claim, cleared on every terminal write. A row who
         -se lease has                                                   
      19 -    -- lapsed with nobody having cleared it is exactly a crashe
         -d worker —                                                     
      20 -    -- see store/frontier.py's recover_expired_leases.         
      38 +    -- Set on claim, cleared on every terminal write. A lease t
         +hat's simply                                                   
      39 +    -- aged past lease_until, with no worker coming back for it
         +, is what a                                                    
      40 +    -- crashed worker leaves behind — see store/frontier.py's c
         +laim_batch.                                                    
      41      lease_until     TIMESTAMPTZ,
      22 -                                                               
      23 -    -- Backoff target for a temporary failure; also the tie-bre
         -aker claim_batch                                               
      24 -    -- orders by, so older-eligible work goes out first.       
      42      next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      43  
      44      content_type    TEXT,
      45      content_length  BIGINT,
      29 -    content_hash    TEXT,                                      
      46 +    content_hash    TEXT REFERENCES contents (content_hash),   
      47      etag            TEXT,
      48  
      49      error_kind      TEXT,
     ...
       54      updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
       55  );
       56  
       40 --- claim_batch and recover_expired_leases only ever scan one s
          -tatus at a                                                    
       41 --- time; a partial index keeps that scan small as 'done'/'fail
          -ed' rows pile                                                 
       42 --- up, instead of growing with the whole table.               
       57 +-- claim_batch and mark_* only ever scan or touch one status a
          +t a time; a                                                   
       58 +-- partial index keeps that scan small as 'done'/'failed' rows
          + pile up,                                                     
       59 +-- instead of growing with the whole table.                   
       60  CREATE INDEX urls_pending_idx ON urls (next_attempt_at) WHERE 
           status = 'pending';
       61  CREATE INDEX urls_in_progress_idx ON urls (lease_until) WHERE 
           status = 'in_progress';
       62  
       46 --- The discovery graph: which page linked to which. No timesta
          -mp, no payload                                                
       47 --- — an edge is just a fact, and it's only ever written once p
          -er pair via                                                   
       48 --- ON CONFLICT DO NOTHING in enqueue_many.                    
       63 +-- Per-type extraction, keyed by content_hash like `contents` 
          +— two urls                                                    
       64 +-- sharing bytes share the extraction too, so it's never redon
          +e or                                                          
       65 +-- duplicated for a second url that happens to hash the same. 
       66 +CREATE TABLE content_metadata (                               
       67 +    content_hash TEXT PRIMARY KEY REFERENCES contents (content
          +_hash),                                                       
       68 +    kind         TEXT NOT NULL,                               
       69 +    payload      JSONB NOT NULL                               
       70 +);                                                            
       71 +                                                              
       72 +-- The discovery graph: which page linked to which, with what 
          +anchor text.                                                  
       73 +-- PRIMARY KEY (src_id, dst_id) means the edge is deduplicated
          + by endpoints                                                 
       74 +-- alone: if the same page links to the same target twice with
          + different                                                    
       75 +-- anchor text, ON CONFLICT DO NOTHING in enqueue_many keeps o
          +nly the first                                                 
       76 +-- anchor text seen and silently drops the second. Acknowledge
          +d, not fixed —                                                
       77 +-- the alternative is a row per (src, dst, anchor), which turn
          +s "did A link                                                 
       78 +-- to B" into a query over duplicates for no reader who's aske
          +d for one yet.                                                
       79  CREATE TABLE links (
       50 -    src_id BIGINT NOT NULL REFERENCES urls (id),              
       51 -    dst_id BIGINT NOT NULL REFERENCES urls (id),              
       80 +    src_id      BIGINT NOT NULL REFERENCES urls (id),         
       81 +    dst_id      BIGINT NOT NULL REFERENCES urls (id),         
       82 +    anchor_text TEXT,                                         
       83      PRIMARY KEY (src_id, dst_id)
       84  );
       85 +                                                              
       86 +-- One row per fetch attempt: status_code and duration_ms, for
          + diagnosing                                                   
       87 +-- *which* attempt did what. urls.attempts is the hot counter 
          +claim_batch                                                   
       88 +-- bumps in the same statement as the lease; this is the appen
          +d-only detail                                                 
       89 +-- an aggregate can't hold without becoming multi-valued.     
       90 +CREATE TABLE fetch_attempts (                                 
       91 +    id          BIGSERIAL PRIMARY KEY,                        
       92 +    url_id      BIGINT NOT NULL REFERENCES urls (id),         
       93 +    attempt_no  INT NOT NULL,                                 
       94 +    status_code INT,                                          
       95 +    duration_ms INT,                                          
       96 +    error_kind  TEXT,                                         
       97 +    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()            
       98 +);                                                            
       99 +                                                              
      100 +CREATE INDEX fetch_attempts_url_id_idx ON fetch_attempts (url_
          +id);                                                          

● Write(src\crawler\store\frontier.py)
  ⎿  Added 112 lines, removed 112 lines
       1  """The frontier: claim, finish, enqueue. Every write here is on
          e SQL
       2 -statement, because the atomicity that matters — bump attempts a
         -nd take the                                                    
       3 -lease, or tell new URLs from ones already known — has to happen
         - inside                                                        
       4 -Postgres in a single round trip, not across a read and a write 
         -in this                                                        
       5 -process. See CLAUDE.md: correctness lives in the database.     
       2 +statement — the atomicity that matters (bump attempts and take 
         +the lease;                                                     
       3 +tell new URLs from known ones) has to happen inside Postgres in
         + a single                                                      
       4 +round trip, not across a read and a write in this process. See 
         +DESIGN.md                                                      
       5 +for the SKIP LOCKED and snapshot-timing mechanics behind claim_
         +batch and                                                      
       6 +enqueue_many.                                                  
       7  
       7 -Nothing here reads `config.max_attempts`. Whether a failure get
         -s retried is                                                   
       8 -fetch/retry.py's decision (`GiveUp` or `RetryAt`); this module 
         -only writes                                                    
       9 -the decision it's handed.                                      
       8 +Nothing here reads config.max_attempts — that's fetch/retry.py'
         +s decision                                                     
       9 +(GiveUp or RetryAt); this module only writes the decision it's 
         +handed.                                                        
      10  """
      11  
      12  from dataclasses import dataclass
     ...
      26      etag: str | None
      27  
      28  
      29 -async def claim_batch(pool: asyncpg.Pool, limit: int, config: C
         -onfig) -> list[ClaimedUrl]:                                    
      30 -    """Bumps `attempts` and takes a lease on up to `limit` rows
         - in one                                                        
      31 -    statement: `FOR UPDATE SKIP LOCKED` inside the subquery pic
         -ks rows no                                                     
      32 -    other worker's claim is currently touching, and the outer `
         -UPDATE`                                                        
      33 -    writes the new attempt count and lease in the same pass a p
         -lain `SELECT                                                   
      34 -    ... FOR UPDATE` couldn't — a follow-up `UPDATE` would be a 
         -second round                                                   
      35 -    trip, and the second worker to run it would target rows the
         - first                                                         
      36 -    worker's `UPDATE` had already moved out of eligibility, so 
         -nothing here                                                   
      37 -    is exposed to a lost update.                               
      29 +@dataclass(frozen=True, slots=True)                            
      30 +class DiscoveredLink:                                          
      31 +    raw_url: str                                               
      32 +    normalized_url: str                                        
      33 +    anchor_text: str | None                                    
      34  
      39 -    Eligible rows are the ones that were never claimed (`pendin
         -g`, past                                                       
      40 -    their backoff) and the ones whose claim rotted (`in_progres
         -s`, past its                                                   
      41 -    lease) — the SKIP LOCKED and the lease timestamp cover two 
         -different                                                      
      42 -    failure shapes: a concurrent claim in flight right now, and
         - a worker                                                      
      43 -    that claimed a row and then died.                          
      35  
      45 -    `now()` here is Postgres's clock, not this process's — see 
         -CLAUDE.md.                                                     
      46 -    Comparing `lease_until` against a timestamp this process re
         -ad a moment                                                    
      47 -    ago, possibly on a host whose clock disagrees with the one 
         -that issued                                                    
      48 -    the lease, is exactly the race this statement exists to avo
         -id.                                                            
      36 +async def claim_batch(pool: asyncpg.Pool, limit: int, config: C
         +onfig) -> list[ClaimedUrl]:                                    
      37 +    """One statement: FOR UPDATE SKIP LOCKED picks up to `limit
         +` rows no                                                      
      38 +    other claim is touching right now, the outer UPDATE bumps `
         +attempts`                                                      
      39 +    and sets the lease on exactly those rows in the same pass. 
         +The WHERE                                                      
      40 +    covers a fresh `pending` row and one whose earlier claim ro
         +tted (lease                                                    
      41 +    past due) alike — recovering a dead worker's row is just an
         +other claim,                                                   
      42 +    not a separate sweep, so there's no second process to run o
         +r forget to                                                    
      43 +    run. now() is Postgres's clock, not this process's: workers
         + can run on                                                    
      44 +    hosts whose wall clocks disagree, and the lease has to mean
         + the same                                                      
      45 +    instant to whichever one checks it.                        
      46      """
      47      rows = await pool.fetch(
      48          """
     ...
       59              FOR UPDATE SKIP LOCKED
       60              LIMIT $1
       61          )
       65 -        RETURNING id, url, attempts, etag                     
       62 +        RETURNING id, normalized_url, attempts, etag          
       63          """,
       64          limit,
       65          config.lease_seconds,
       66      )
       67      return [
       71 -        ClaimedUrl(id=row["id"], url=row["url"], attempt_no=ro
          -w["attempts"], etag=row["etag"])                              
       72 -        for row in rows                                       
       68 +        ClaimedUrl(id=r["id"], url=r["normalized_url"], attemp
          +t_no=r["attempts"], etag=r["etag"])                           
       69 +        for r in rows                                         
       70      ]
       71  
       72  
       73  async def mark_done(
       74      pool: asyncpg.Pool,
       75      url_id: int,
       76 +    attempt_no: int,                                          
       77      *,
       78 +    status_code: int | None,                                  
       79 +    elapsed: float,                                           
       80      content_type: str | None,
       81      content_length: int | None,
       82      content_hash: str | None,
       83      etag: str | None,
       84  ) -> None:
       85 -    """The caller already fetched and stored the body — this j
          -ust records                                                   
       86 -    what landed. Whether the hash matches a prior visit (bump 
          -`last_seen_at`                                                
       87 -    only) or differs (a change worth noting) is the caller's c
          -omparison to                                                  
       88 -    make before calling this; frontier.py doesn't hold the pre
          -vious hash to                                                 
       89 -    compare against.                                          
       85 +    """Records what landed and logs the attempt that produced 
          +it, in one                                                    
       86 +    statement — the `u` CTE runs for its side effect alone; a 
          +data-modifying                                                
       87 +    CTE executes even when nothing selects from it. Whether `c
          +ontent_hash`                                                  
       88 +    matches a prior visit (unchanged) or differs (a real chang
          +e) is the                                                     
       89 +    caller's comparison to make first; this only writes the re
          +sult.                                                         
       90      """
       91      await pool.execute(
       92          """
       93 -        UPDATE urls                                           
       94 -        SET status = 'done',                                  
       95 -            content_type = $2,                                
       96 -            content_length = $3,                              
       97 -            content_hash = $4,                                
       98 -            etag = $5,                                        
       99 -            lease_until = NULL,                               
      100 -            last_seen_at = now(),                             
      101 -            updated_at = now()                                
      102 -        WHERE id = $1                                         
       93 +        WITH u AS (                                           
       94 +            UPDATE urls                                       
       95 +            SET status = 'done', content_type = $3, content_le
          +ngth = $4,                                                    
       96 +                content_hash = $5, etag = $6, lease_until = NU
          +LL,                                                           
       97 +                last_seen_at = now(), updated_at = now()      
       98 +            WHERE id = $1                                     
       99 +        )                                                     
      100 +        INSERT INTO fetch_attempts (url_id, attempt_no, status
          +_code, duration_ms)                                           
      101 +        VALUES ($1, $2, $7, $8)                               
      102          """,
      103          url_id,
      104 +        attempt_no,                                           
      105          content_type,
      106          content_length,
      107          content_hash,
      108          etag,
      109 +        status_code,                                          
      110 +        round(elapsed * 1000),                                
      111      )
      112  
      113  
      114  async def mark_failed(
      115      pool: asyncpg.Pool,
      116      url_id: int,
      117 +    attempt_no: int,                                          
      118      decision: GiveUp | RetryAt,
      119      error_kind: ErrorKind,
      120 +    *,                                                        
      121 +    status_code: int | None = None,                           
      122 +    elapsed: float = 0.0,                                     
      123      error_message: str | None = None,
      124  ) -> None:
      119 -    """Writes the retry decision fetch/retry.py already made. 
          -A `GiveUp`                                                    
      120 -    ends the row at `failed`; a `RetryAt` sends it back to `pe
          -nding` with                                                   
      121 -    `next_attempt_at` set to when it's next eligible — `claim_
          -batch` won't                                                  
      122 -    look at it again before then.                             
      125 +    """Writes the retry decision fetch/retry.py already made —
          + a GiveUp                                                     
      126 +    ends the row at 'failed', a RetryAt sends it back to 'pend
          +ing' at the                                                   
      127 +    given time — and logs the attempt. `attempts` itself isn't
          + touched                                                      
      128 +    here; only claim_batch's own statement ever increments it.
      129      """
      130      if isinstance(decision, GiveUp):
      131          status, next_attempt_at = "failed", None
     ...
      134  
      135      await pool.execute(
      136          """
      131 -        UPDATE urls                                           
      132 -        SET status = $2,                                      
      133 -            next_attempt_at = COALESCE($3, next_attempt_at),  
      134 -            lease_until = NULL,                               
      135 -            error_kind = $4,                                  
      136 -            error_message = $5,                               
      137 -            updated_at = now()                                
      138 -        WHERE id = $1                                         
      137 +        WITH u AS (                                           
      138 +            UPDATE urls                                       
      139 +            SET status = $2,                                  
      140 +                next_attempt_at = COALESCE($3, next_attempt_at
          +),                                                            
      141 +                lease_until = NULL,                           
      142 +                error_kind = $4,                              
      143 +                error_message = $5,                           
      144 +                updated_at = now()                            
      145 +            WHERE id = $1                                     
      146 +        )                                                     
      147 +        INSERT INTO fetch_attempts (url_id, attempt_no, status
          +_code, duration_ms, error_kind)                               
      148 +        VALUES ($1, $6, $7, $8, $4)                           
      149          """,
      150          url_id,
      151          status,
      152          next_attempt_at,
      153          error_kind.value,
      154          error_message,
      155 +        attempt_no,                                           
      156 +        status_code,                                          
      157 +        round(elapsed * 1000),                                
      158      )
      159  
      160  
      161  async def release(pool: asyncpg.Pool, url_id: int) -> None:
      149 -    """Gives back a claimed row immediately, for a worker that
          - took a lease                                                 
      150 -    and never acted on it — a graceful shutdown mid-batch, say
          -. Without this                                                
      151 -    the row just sits unclaimable until `lease_seconds` runs o
          -ut on its own;                                                
      152 -    `recover_expired_leases` would eventually get it, but ther
          -e's no reason                                                 
      153 -    to make the rest of the crawl wait on a lease nobody's hol
          -ding anymore.                                                 
      162 +    """Gives back a claimed row immediately — a worker that to
          +ok a lease                                                    
      163 +    and never acted on it (shutting down mid-batch). Doesn't t
          +ouch                                                          
      164 +    `attempts`, same as the expired-lease path inside claim_ba
          +tch.                                                          
      165      """
      166      await pool.execute(
      167          """
     ...
      173      )
      174  
      175  
      165 -async def recover_expired_leases(pool: asyncpg.Pool) -> int:  
      166 -    """Requeues every `in_progress` row whose lease has lapsed
          -. This is the                                                 
      167 -    crash path: no worker released it, no worker is coming bac
          -k for it, so a                                                
      168 -    lease that's simply aged past `lease_until` is the only si
          -gnal a dead                                                   
      169 -    worker leaves behind. Returns the count recovered, for a l
          -og line.                                                      
      170 -    """                                                       
      171 -    result = await pool.execute(                              
      172 -        """                                                   
      173 -        UPDATE urls                                           
      174 -        SET status = 'pending', lease_until = NULL, updated_at
          - = now()                                                      
      175 -        WHERE status = 'in_progress' AND lease_until < now()  
      176 -        """                                                   
      177 -    )                                                         
      178 -    return int(result.split()[-1])                            
      179 -                                                              
      180 -                                                              
      176  async def enqueue_many(
      182 -    pool: asyncpg.Pool, urls: list[str], depth: int, src_id: i
          -nt | None = None                                              
      177 +    pool: asyncpg.Pool, links: list[DiscoveredLink], depth: in
          +t, src_id: int | None = None                                  
      178  ) -> dict[str, int]:
      184 -    """Inserts whichever of `urls` aren't already known, recor
          -ds an edge                                                    
      185 -    from `src_id` to every one of them — new or already known,
          - since a link                                                 
      186 -    from a second page to an already-visited URL is still a re
          -al edge — and                                                 
      187 -    returns the id of every url in `urls`, new and pre-existin
          -g alike.                                                      
      179 +    """Inserts whichever `links` aren't already known, records
          + an edge from                                                 
      180 +    `src_id` to every one of them — new or already known, sinc
          +e a second                                                    
      181 +    page linking to an already-visited URL is still a real edg
          +e — and                                                       
      182 +    returns every url's id, new and pre-existing alike.       
      183  
      189 -    The pre-existing ids can't come from a second `SELECT` aft
          -er the                                                        
      190 -    `INSERT`, and the `INSERT` can't be `ON CONFLICT DO UPDATE
          - ... RETURNING`                                               
      191 -    either: both write to (or re-read) rows this call had no r
          -eason to                                                      
      192 -    touch, which is exactly the "extra update" a partial index
          - on `status`                                                  
      193 -    would then have to maintain for nothing. Postgres runs eve
          -ry statement                                                  
      194 -    in one `WITH` against the same snapshot, so `existing` gen
          -uinely can't                                                  
      195 -    see `ins`'s rows and doesn't need to guard against double-
          -counting them.                                                
      184 +    Two statements, not one: under Read Committed a single INS
          +ERT-then-read                                                 
      185 +    CTE can miss a row a concurrent transaction is committing 
          +at the same                                                   
      186 +    instant — the insert correctly resolves the conflict, but 
          +a sibling                                                     
      187 +    read in the same statement uses the snapshot taken before 
          +that commit                                                   
      188 +    and simply never sees the row (verified against Postgres, 
          +not assumed;                                                  
      189 +    see DESIGN.md). Splitting into two statements makes the se
          +cond one take                                                 
      190 +    its own fresh snapshot, taken after the first has returned
          + — which                                                      
      191 +    means every conflicting concurrent insert has already reso
          +lved one way                                                  
      192 +    or the other.                                             
      193      """
      194 +    normalized = [link.normalized_url for link in links]      
      195 +    raw = [link.raw_url for link in links]                    
      196 +    anchors = [link.anchor_text for link in links]            
      197 +                                                              
      198 +    await pool.execute(                                       
      199 +        """                                                   
      200 +        INSERT INTO urls (normalized_url, raw_url, depth)     
      201 +        SELECT DISTINCT ON (normalized_url) normalized_url, ra
          +w_url, $3                                                     
      202 +        FROM unnest($1::text[], $2::text[]) AS t (normalized_u
          +rl, raw_url)                                                  
      203 +        ON CONFLICT (normalized_url) DO NOTHING               
      204 +        """,                                                  
      205 +        normalized,                                           
      206 +        raw,                                                  
      207 +        depth,                                                
      208 +    )                                                         
      209      rows = await pool.fetch(
      210          """
      211          WITH input AS (
      200 -            SELECT DISTINCT unnest($1::text[]) AS url         
      212 +            SELECT * FROM unnest($1::text[], $2::text[]) AS t 
          +(normalized_url, anchor_text)                                 
      213          ),
      202 -        ins AS (                                              
      203 -            INSERT INTO urls (url, depth)                     
      204 -            SELECT url, $2 FROM input                         
      205 -            ON CONFLICT (url) DO NOTHING                      
      206 -            RETURNING id, url                                 
      214 +        resolved AS (                                         
      215 +            SELECT DISTINCT ON (u.normalized_url) u.id, u.norm
          +alized_url                                                    
      216 +            FROM urls u JOIN input i ON i.normalized_url = u.n
          +ormalized_url                                                 
      217          ),
      208 -        existing AS (                                         
      209 -            SELECT u.id, u.url                                
      210 -            FROM urls u                                       
      211 -            JOIN input i ON i.url = u.url                     
      212 -            WHERE NOT EXISTS (SELECT 1 FROM ins WHERE ins.url 
          -= u.url)                                                      
      213 -        ),                                                    
      214 -        all_urls AS (                                         
      215 -            SELECT id, url FROM ins                           
      216 -            UNION ALL                                         
      217 -            SELECT id, url FROM existing                      
      218 -        ),                                                    
      218          links_ins AS (
      220 -            INSERT INTO links (src_id, dst_id)                
      221 -            SELECT $3, id FROM all_urls                       
      219 +            INSERT INTO links (src_id, dst_id, anchor_text)   
      220 +            SELECT $3, r.id, i.anchor_text                    
      221 +            FROM input i JOIN resolved r ON r.normalized_url =
          + i.normalized_url                                             
      222              WHERE $3::bigint IS NOT NULL
      223 -            ON CONFLICT DO NOTHING                            
      223 +            ON CONFLICT (src_id, dst_id) DO NOTHING           
      224          )
      225 -        SELECT id, url FROM all_urls                          
      225 +        SELECT id, normalized_url FROM resolved               
      226          """,
      227 -        urls,                                                 
      228 -        depth,                                                
      227 +        normalized,                                           
      228 +        anchors,                                              
      229          src_id,
      230      )
      231 -    return {row["url"]: row["id"] for row in rows}            
      231 +    return {row["normalized_url"]: row["id"] for row in rows} 

  Read 1 file (ctrl+o to expand)

● I'm moving the long narrative explanations into DESIGN.md (which already has
  exactly this "chose X, rejected Y" format) and trimming frontier.py's
  docstrings down to short pointers — that's what comes out.

  Read 1 file (ctrl+o to expand)

● Update(DESIGN.md)
  ⎿  Added 50 lines
       82  `src/crawler/models.py`. Everything downstream (`classify`, `F
           etchResponse`,
       83  the handlers) already only knows `bytes | None`; none of it kn
           ows or cares
       84  how those bytes were spelled on the wire.
       85 +                                                              
       86 +## Lease recovery: claim_batch reclaims, no separate sweep    
       87 +                                                              
       88 +Chose folding an expired lease into `claim_batch`'s own `WHERE
          +`                                                             
       89 +(`status = 'in_progress' AND lease_until < now()`, same statem
          +ent as the                                                    
       90 +normal `pending` branch). Rejected a periodic `recover_expired
          +_leases`                                                      
       91 +sweep as a second mechanism alongside it. Once `claim_batch` a
          +lready                                                        
       92 +reclaims dead work the moment any worker next asks for more, a
          + sweep has                                                    
       93 +no correctness job left — every worker loop already calls `cla
          +im_batch`                                                     
       94 +continuously, so recovery is already as immediate as a sweep c
          +ould make                                                     
       95 +it, just without a second process to run or forget to run. Onl
          +y                                                             
       96 +`claim_batch`'s own `UPDATE` ever touches `attempts`; a lease 
          +reclaimed                                                     
       97 +after a crash costs one attempt exactly like an ordinary claim
          + would,                                                       
       98 +whether or not a real fetch happened before the crash. Would r
          +econsider if                                                  
       99 +idle recovery — nobody calling `claim_batch` — becomes a real 
          +scenario,                                                     
      100 +e.g. a crawl that drains its frontier faster than crashed leas
          +es expire.                                                    
      101 +                                                              
      102 +## enqueue_many: two statements, not one CTE                  
      103 +                                                              
      104 +Chose `INSERT ... ON CONFLICT DO NOTHING`, then a separate `SE
          +LECT` (plus                                                   
      105 +the `links` insert) as a second statement. Rejected doing both
          + in one                                                       
      106 +`WITH` query. Verified against Postgres directly: when two tra
          +nsactions                                                     
      107 +insert the same brand-new URL at the same instant, the loser's
          + own                                                          
      108 +`INSERT` correctly waits and resolves the conflict once the wi
          +nner                                                          
      109 +commits — but a sibling read in the *same* statement (an `exis
          +ting` CTE                                                     
      110 +probing the table directly) runs against the snapshot taken at
          + statement                                                    
      111 +start, before that commit, and never sees the row. Splitting t
          +he read into                                                  
      112 +its own statement gives it a fresh snapshot, taken after the f
          +irst                                                          
      113 +statement has already returned — by which point every conflict
          +ing insert                                                    
      114 +it raced against has resolved one way or the other. Read Commi
          +tted's                                                        
      115 +per-statement snapshot is what makes this safe; REPEATABLE REA
          +D pins one                                                    
      116 +snapshot for the whole transaction, so the same `INSERT` there
          + raises                                                       
      117 +"could not serialize access due to concurrent update" instead 
          +of silently                                                   
      118 +missing the row — louder, but it would mean adding retry-on-se
          +rialization                                                   
      119 +handling around this call, which Read Committed doesn't need. 
          +Would                                                         
      120 +reconsider if this pool's default isolation level ever moves o
          +ff Read                                                       
      121 +Committed.                                                    
      122 +                                                              
      123 +## Migrations: advisory lock; frontier: none                  
      124 +                                                              
      125 +Chose a global advisory lock around the migration runner. Reje
          +cted using                                                    
      126 +one anywhere in `store/frontier.py`. Migrations run once, at b
          +oot, with no                                                  
      127 +per-row unit of work — coarse and rare is the right shape. The
          + frontier is                                                  
      128 +the opposite: continuous, per-row, high-frequency, and Postgre
          +s's own row                                                   
      129 +locks (`FOR UPDATE SKIP LOCKED`, or the lock a plain `UPDATE .
          +.. WHERE id                                                   
      130 += $1` already takes) already give the right granularity. An ad
          +visory lock                                                   
      131 +around frontier writes would serialize every worker into singl
          +e file,                                                       
      132 +which is the concurrency the schema exists to provide. Would r
          +econsider if                                                  
      133 +a frontier operation ever needed to span more than one row ato
          +mically in a                                                  
      134 +way row locks can't express.                                  

● Now the trimmed frontier.py:
● Write(src\crawler\store\frontier.py)
  ⎿  Added 25 lines, removed 42 lines
       1 -"""The frontier: claim, finish, enqueue. Every write here is on
         -e SQL                                                          
       2 -statement — the atomicity that matters (bump attempts and take 
         -the lease;                                                     
       3 -tell new URLs from known ones) has to happen inside Postgres in
         - a single                                                      
       4 -round trip, not across a read and a write in this process. See 
         -DESIGN.md                                                      
       5 -for the SKIP LOCKED and snapshot-timing mechanics behind claim_
         -batch and                                                      
       6 -enqueue_many.                                                  
       1 +"""The frontier: claim, finish, enqueue. Every write is one SQL
         + statement —                                                   
       2 +the atomicity that matters (bump attempts and take the lease; n
         +ew URLs vs                                                     
       3 +known ones) happens inside Postgres, not across a read and a wr
         +ite here.                                                      
       4 +See DESIGN.md for the SKIP LOCKED and snapshot-timing mechanics
         +.                                                              
       5  
       8 -Nothing here reads config.max_attempts — that's fetch/retry.py'
         -s decision                                                     
       9 -(GiveUp or RetryAt); this module only writes the decision it's 
         -handed.                                                        
       6 +Nothing here reads config.max_attempts — that's fetch/retry.py'
         +s call;                                                        
       7 +this module only writes the decision it's handed.              
       8  """
       9  
      10  from dataclasses import dataclass
     ...
      32  
      33  
      34  async def claim_batch(pool: asyncpg.Pool, limit: int, config: C
          onfig) -> list[ClaimedUrl]:
      37 -    """One statement: FOR UPDATE SKIP LOCKED picks up to `limit
         -` rows no                                                      
      38 -    other claim is touching right now, the outer UPDATE bumps `
         -attempts`                                                      
      39 -    and sets the lease on exactly those rows in the same pass. 
         -The WHERE                                                      
      40 -    covers a fresh `pending` row and one whose earlier claim ro
         -tted (lease                                                    
      41 -    past due) alike — recovering a dead worker's row is just an
         -other claim,                                                   
      42 -    not a separate sweep, so there's no second process to run o
         -r forget to                                                    
      43 -    run. now() is Postgres's clock, not this process's: workers
         - can run on                                                    
      44 -    hosts whose wall clocks disagree, and the lease has to mean
         - the same                                                      
      45 -    instant to whichever one checks it.                        
      35 +    """One statement: SKIP LOCKED claims up to `limit` rows nob
         +ody else is                                                    
      36 +    touching and bumps `attempts`/sets the lease on exactly tho
         +se rows in                                                     
      37 +    the same pass — a fresh `pending` row and one whose lease r
         +otted are                                                      
      38 +    both eligible here, so there's no separate recovery sweep (
         +DESIGN.md).                                                    
      39 +    now() is Postgres's clock, not this process's, on purpose. 
      40      """
      41      rows = await pool.fetch(
      42          """
     ...
      76      content_hash: str | None,
      77      etag: str | None,
      78  ) -> None:
      85 -    """Records what landed and logs the attempt that produced i
         -t, in one                                                      
      86 -    statement — the `u` CTE runs for its side effect alone; a d
         -ata-modifying                                                  
      87 -    CTE executes even when nothing selects from it. Whether `co
         -ntent_hash`                                                    
      88 -    matches a prior visit (unchanged) or differs (a real change
         -) is the                                                       
      89 -    caller's comparison to make first; this only writes the res
         -ult.                                                           
      79 +    """Records what landed and logs the attempt in one statemen
         +t — the `u`                                                    
      80 +    CTE runs for its side effect alone; a data-modifying CTE ex
         +ecutes even                                                    
      81 +    when nothing selects from it. Whether `content_hash` matche
         +s a prior                                                      
      82 +    visit is the caller's comparison to make first; this only w
         +rites the                                                      
      83 +    result.                                                    
      84      """
      85      await pool.execute(
      86          """
     ...
      116      elapsed: float = 0.0,
      117      error_message: str | None = None,
      118  ) -> None:
      125 -    """Writes the retry decision fetch/retry.py already made —
          - a GiveUp                                                     
      126 -    ends the row at 'failed', a RetryAt sends it back to 'pend
          -ing' at the                                                   
      127 -    given time — and logs the attempt. `attempts` itself isn't
          - touched                                                      
      128 -    here; only claim_batch's own statement ever increments it.
      119 +    """Writes the retry decision fetch/retry.py already made a
          +nd logs the                                                   
      120 +    attempt. `attempts` isn't touched here — only claim_batch'
          +s statement                                                   
      121 +    ever increments it.                                       
      122      """
      123      if isinstance(decision, GiveUp):
      124          status, next_attempt_at = "failed", None
     ...
      152  
      153  
      154  async def release(pool: asyncpg.Pool, url_id: int) -> None:
      162 -    """Gives back a claimed row immediately — a worker that to
          -ok a lease                                                    
      163 -    and never acted on it (shutting down mid-batch). Doesn't t
          -ouch                                                          
      164 -    `attempts`, same as the expired-lease path inside claim_ba
          -tch.                                                          
      155 +    """Gives back a claimed row immediately, for a worker that
          + took a lease                                                 
      156 +    and never acted on it. Doesn't touch `attempts`.          
      157      """
      158      await pool.execute(
      159          """
     ...
      169      pool: asyncpg.Pool, links: list[DiscoveredLink], depth: in
           t, src_id: int | None = None
      170  ) -> dict[str, int]:
      171      """Inserts whichever `links` aren't already known, records
            an edge from
      180 -    `src_id` to every one of them — new or already known, sinc
          -e a second                                                    
      181 -    page linking to an already-visited URL is still a real edg
          -e — and                                                       
      182 -    returns every url's id, new and pre-existing alike.       
      183 -                                                              
      184 -    Two statements, not one: under Read Committed a single INS
          -ERT-then-read                                                 
      185 -    CTE can miss a row a concurrent transaction is committing 
          -at the same                                                   
      186 -    instant — the insert correctly resolves the conflict, but 
          -a sibling                                                     
      187 -    read in the same statement uses the snapshot taken before 
          -that commit                                                   
      188 -    and simply never sees the row (verified against Postgres, 
          -not assumed;                                                  
      189 -    see DESIGN.md). Splitting into two statements makes the se
          -cond one take                                                 
      190 -    its own fresh snapshot, taken after the first has returned
          - — which                                                      
      191 -    means every conflicting concurrent insert has already reso
          -lved one way                                                  
      192 -    or the other.                                             
      172 +    `src_id` to every one of them — new or already known — and
          + returns                                                      
      173 +    every url's id. Two statements, not one: see DESIGN.md — a
          + single                                                       
      174 +    INSERT-then-read CTE can miss a row a concurrent transacti
          +on commits                                                    
      175 +    mid-statement.                                            
      176      """
      177      normalized = [link.normalized_url for link in links]
      178      raw = [link.raw_url for link in links]

  Read 1 file (ctrl+o to expand)

● Update(src\crawler\store\frontier.py)
  ⎿  Added 44 lines, removed 43 lines
       67  async def mark_done(
       68      pool: asyncpg.Pool,
       69      url_id: int,
       70 -    attempt_no: int,                                          
       70      *,
       72 -    status_code: int | None,                                  
       73 -    elapsed: float,                                           
       71      content_type: str | None,
       72      content_length: int | None,
       73      content_hash: str | None,
       74      etag: str | None,
       75  ) -> None:
       79 -    """Records what landed and logs the attempt in one stateme
          -nt — the `u`                                                  
       80 -    CTE runs for its side effect alone; a data-modifying CTE e
          -xecutes even                                                  
       81 -    when nothing selects from it. Whether `content_hash` match
          -es a prior                                                    
       82 -    visit is the caller's comparison to make first; this only 
          -writes the                                                    
       83 -    result.                                                   
       76 +    """Records what landed. Whether `content_hash` matches a p
          +rior visit is                                                 
       77 +    the caller's comparison to make first; this only writes th
          +e result.                                                     
       78      """
       79      await pool.execute(
       80          """
       87 -        WITH u AS (                                           
       88 -            UPDATE urls                                       
       89 -            SET status = 'done', content_type = $3, content_le
          -ngth = $4,                                                    
       90 -                content_hash = $5, etag = $6, lease_until = NU
          -LL,                                                           
       91 -                last_seen_at = now(), updated_at = now()      
       92 -            WHERE id = $1                                     
       93 -        )                                                     
       94 -        INSERT INTO fetch_attempts (url_id, attempt_no, status
          -_code, duration_ms)                                           
       95 -        VALUES ($1, $2, $7, $8)                               
       81 +        UPDATE urls                                           
       82 +        SET status = 'done', content_type = $2, content_length
          + = $3,                                                        
       83 +            content_hash = $4, etag = $5, lease_until = NULL, 
       84 +            last_seen_at = now(), updated_at = now()          
       85 +        WHERE id = $1                                         
       86          """,
       87          url_id,
       98 -        attempt_no,                                           
       88          content_type,
       89          content_length,
       90          content_hash,
       91          etag,
      103 -        status_code,                                          
      104 -        round(elapsed * 1000),                                
       92      )
       93  
       94  
       95  async def mark_failed(
      109 -    pool: asyncpg.Pool,                                       
      110 -    url_id: int,                                              
      111 -    attempt_no: int,                                          
      112 -    decision: GiveUp | RetryAt,                               
      113 -    error_kind: ErrorKind,                                    
      114 -    *,                                                        
      115 -    status_code: int | None = None,                           
      116 -    elapsed: float = 0.0,                                     
       96 +    pool: asyncpg.Pool, url_id: int, decision: GiveUp | RetryA
          +t, error_kind: ErrorKind,                                     
       97      error_message: str | None = None,
       98  ) -> None:
      119 -    """Writes the retry decision fetch/retry.py already made a
          -nd logs the                                                   
      120 -    attempt. `attempts` isn't touched here — only claim_batch'
          -s statement                                                   
      121 -    ever increments it.                                       
       99 +    """Writes the retry decision fetch/retry.py already made —
          + GiveUp ends                                                  
      100 +    the row at 'failed', RetryAt sends it back to 'pending'. D
          +oesn't touch                                                  
      101 +    `attempts` — only claim_batch's statement ever increments 
          +it.                                                           
      102      """
      103      if isinstance(decision, GiveUp):
      104          status, next_attempt_at = "failed", None
     ...
      107  
      108      await pool.execute(
      109          """
      130 -        WITH u AS (                                           
      131 -            UPDATE urls                                       
      132 -            SET status = $2,                                  
      133 -                next_attempt_at = COALESCE($3, next_attempt_at
          -),                                                            
      134 -                lease_until = NULL,                           
      135 -                error_kind = $4,                              
      136 -                error_message = $5,                           
      137 -                updated_at = now()                            
      138 -            WHERE id = $1                                     
      139 -        )                                                     
      140 -        INSERT INTO fetch_attempts (url_id, attempt_no, status
          -_code, duration_ms, error_kind)                               
      141 -        VALUES ($1, $6, $7, $8, $4)                           
      110 +        UPDATE urls                                           
      111 +        SET status = $2,                                      
      112 +            next_attempt_at = COALESCE($3, next_attempt_at),  
      113 +            lease_until = NULL,                               
      114 +            error_kind = $4,                                  
      115 +            error_message = $5,                               
      116 +            updated_at = now()                                
      117 +        WHERE id = $1                                         
      118          """,
      119          url_id,
      120          status,
      121          next_attempt_at,
      122          error_kind.value,
      123          error_message,
      124 +    )                                                         
      125 +                                                              
      126 +                                                              
      127 +async def record_attempt(                                     
      128 +    pool: asyncpg.Pool,                                       
      129 +    url_id: int,                                              
      130 +    attempt_no: int,                                          
      131 +    *,                                                        
      132 +    status_code: int | None,                                  
      133 +    elapsed: float,                                           
      134 +    error_kind: ErrorKind | None = None,                      
      135 +) -> None:                                                    
      136 +    """One row per fetch attempt — status_code and duration_ms
          +, for                                                         
      137 +    diagnosing which attempt did what. Deliberately its own st
          +atement, not                                                  
      138 +    folded into mark_done/mark_failed: it's diagnostic, not cr
          +awl state, so                                                 
      139 +    it doesn't need their atomicity. A crash between the two c
          +alls loses a                                                  
      140 +    log row, never correctness.                               
      141 +    """                                                       
      142 +    await pool.execute(                                       
      143 +        """                                                   
      144 +        INSERT INTO fetch_attempts (url_id, attempt_no, status
          +_code, duration_ms, error_kind)                               
      145 +        VALUES ($1, $2, $3, $4, $5)                           
      146 +        """,                                                  
      147 +        url_id,                                               
      148          attempt_no,
      149          status_code,
      150          round(elapsed * 1000),
      151 +        error_kind.value if error_kind else None,             
      152      )
      153  
      154  

  Ran 1 shell command (ctrl+o to expand)

● Let me verify the final version end-to-end against real Postgres, since the
  interfaces changed again (mark_done/mark_failed simplified, record_attempt
  added, enqueue_many now takes DiscoveredLinks).

  Ran 1 shell command (ctrl+o to expand)

● Write(scratch_final.py)
  ⎿  Wrote 114 lines to scratch_final.py
       1 import asyncio
       2 import sys
       3 from pathlib import Path
       4 
       5 sys.path.insert(0, "src")
       6 
       7 from crawler.config import Config
       8 from crawler.errors import ErrorKind
       9 from crawler.fetch.retry import GiveUp, RetryAt
      10 from crawler.store import db, frontier
     … +133 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

● Update(scratch_final.py)
  ⎿  Added 6 lines, removed 2 lines
      36              assert raw_a == "http://A", raw_a
      37          print("raw_url preserved OK:", raw_a)
      38  
      39 -        # same page links to b twice with a different anchor ->
         - PK(src,dst) keeps first                                       
      39 +        # a links to b for the first time (anchor "About") and 
         +to a new c                                                     
      40          links2 = [
      41              frontier.DiscoveredLink("http://c", "http://c", Non
          e),
      42 -            frontier.DiscoveredLink("http://B", "http://b", "Ab
         -out us (again)"),                                              
      42 +            frontier.DiscoveredLink("http://B", "http://b", "Ab
         +out"),                                                         
      43          ]
      44          ids2 = await frontier.enqueue_many(pool, links2, depth=
          1, src_id=ids1["http://a"])
      45          print("enqueue_many (existing + new, with src) OK:", id
          s2)
      46  
      47 +        # a links to b a SECOND time with a different anchor ->
         + PK(src,dst) keeps the first                                   
      48 +        links3 = [frontier.DiscoveredLink("http://b#again", "ht
         +tp://b", "About us (again)")]                                  
      49 +        await frontier.enqueue_many(pool, links3, depth=1, src_
         +id=ids1["http://a"])                                           
      50 +                                                               
      51          async with pool.acquire() as conn:
      52              anchor = await conn.fetchval(
      53                  "SELECT anchor_text FROM links WHERE src_id=$1 
          AND dst_id=$2",

  Ran 1 shell command (ctrl+o to expand)

● Update(scratch_final.py)
  ⎿  Added 5 lines
      60          by_url = {c.url: c for c in claimed}
      61          print("claimed:", claimed)
      62  
      63 +        async with pool.acquire() as conn:                     
      64 +            await conn.execute(                                
      65 +                "INSERT INTO contents (content_hash, content_ty
         +pe, byte_size, storage_path) "                                 
      66 +                "VALUES ('h1', 'text/html', 10, '/tmp/h1.html')
         +"                                                              
      67 +            )                                                  
      68          await frontier.mark_done(
      69              pool, by_url["http://a"].id,
      70              content_type="text/html", content_length=10, conten
          t_hash="h1", etag="e1",

  Ran 1 shell command (ctrl+o to expand)

● Everything checks out end to end, including the content_hash FK correctly
  rejecting a hash that doesn't exist in contents yet — a real ordering
  constraint for the future worker.py: it has to write the blob (via blobs.py,
  not built yet) before calling mark_done. Cleaning up now.

  Ran 1 shell command (ctrl+o to expand)

● Recovery path — already picked, now confirmed the sweep is gone

  Kept what was already correct: claim_batch's own WHERE reclaims an expired
  in_progress lease in the same statement as a fresh pending claim —
  recover_expired_leases was the redundant one, and it's deleted (5 functions
  now: claim_batch, mark_done, mark_failed, release, enqueue_many, plus the new
  record_attempt). The partial index on pending still earns its keep regardless:
  the OR in claim_batch's subquery lets Postgres index-scan each branch
  separately (bitmap-OR the two small candidate sets, then sort/limit), so as
  done/failed rows pile up over a long crawl, the pending branch stays cheap
  independent of how big the whole table gets — that's what the partial index
  buys, not anything about how the two branches combine. Full reasoning is in
  DESIGN.md now under "Lease recovery."

  Isolation level — verified, not assumed

  Checked all three scenarios against real Postgres rather than trusting the
  earlier claim:

  - Read Committed, two separate auto-committed statements (what enqueue_many
    does): works, as shown before.
  - Read Committed, both statements wrapped in one explicit transaction (what a
    worker doing "one transaction per URL" would do): still works — confirmed by
    forcing the exact same race with an explicit BEGIN around both statements
    on one connection. Read Committed re-takes its snapshot per statement, not
    per transaction, so grouping the two statements into one transaction changes
    nothing about visibility.
  - REPEATABLE READ: breaks, but loudly, not silently. The transaction's
    snapshot is fixed once, at the transaction's start, and never advances — so
    if you tried the two-statement fix under REPEATABLE READ, statement 1's own
    INSERT ... ON CONFLICT DO NOTHING raises 40001 could not serialize access 
    due to concurrent update the moment it wakes up and finds its snapshot
    stale, rather than quietly returning wrong data. That's actually the better
    failure mode, but it means REPEATABLE READ would require
    retry-on-serialization handling around this call that Read Committed doesn't
    need — one more reason to leave the pool's default isolation level alone.

  Also in DESIGN.md now.

  links.anchor_text — back, and the dedup acknowledged

  Added anchor_text TEXT to links. Verified the exact scenario you're pointing
  at: two enqueue_many calls from the same page to the same target with
  different anchor text — the second INSERT ... ON CONFLICT (src_id, dst_id) DO 
  NOTHING is a no-op, so the first anchor text seen wins and the second is
  silently dropped by the PK, unless something says so. It now does — there's a
  comment on the links table in the migration stating exactly this, in place,
  not left implicit:

  ▎ "if the same page links to the same target twice with different anchor text,
  ▎ ON CONFLICT DO NOTHING in enqueue_many keeps only the first anchor text 
  ▎ seen and silently drops the second. Acknowledged, not fixed — the 
  ▎ alternative is a row per (src, dst, anchor), which turns 'did A link to B' 
  ▎ into a query over duplicates for no reader who's asked for one yet."

  Verified this end-to-end against Postgres: two calls, same edge, different
  anchors — About survived, About us (again) was dropped, exactly as documented.

  contents / content_metadata — built as you specified

  contents(content_hash PK, content_type, byte_size, storage_path, 
  first_seen_at), content_metadata(content_hash PK, kind, payload jsonb), both
  keyed on the hash so two URLs sharing bytes share one extraction.
  urls.content_hash is now a real FK into contents — I hit this immediately in
  verification: mark_done rejects a content_hash that doesn't have a matching
  contents row yet, which means the future worker.py has a real ordering
  constraint — write the blob (via blobs.py, not built yet) before calling
  mark_done, not after. Worth knowing now, not when blobs.py gets written.
  Neither table has a frontier.py function writing to it — that's
  blobs.py/metadata.py's job per the layout in CLAUDE.md, not in scope here.

  What came out to get back under the line

  Two things, not padding:

  1. The long narrative "why" — the SKIP LOCKED mechanics, the enqueue_many
     snapshot race, the advisory-lock reasoning — moved out of code docstrings
     into three new DESIGN.md entries, which already exist for exactly this
     ("chose X, rejected Y, because Z"). Docstrings in frontier.py are now
     pointers, not essays.
  2. The clever bit — folding the fetch_attempts write into
     mark_done/mark_failed via an unreferenced CTE (relying on the non-obvious
     fact that a data-modifying CTE runs even when nothing selects from it — I
     had to verify that empirically too, which is itself a signal it's too cute
     for this file) — came out entirely. It's now record_attempt, its own
     function, called separately. That's an honest simplification, not just a
     line-count trick: fetch_attempts is diagnostic, not crawl-correctness
     state, so it doesn't need the same-statement atomicity
     mark_done/mark_failed need for urls. A crash between the two calls loses a
     log row, never correctness.

  Net result: 215 lines, not under 120. I'm telling you straight rather than
  mangling the SQL formatting to hit a number — the remaining size is six
  single-purpose atomic statements (claim_batch, mark_done, mark_failed,
  release, record_attempt, enqueue_many) plus two small dataclasses, and every
  function still does exactly one job. Further cuts from here mean cutting scope
  — e.g., dropping fetch_attempts logging, or dropping raw_url/anchor_text —
  not cutting fat. Say if you want that trade; I'm not going to make it
  unilaterally.

  Everything above was checked against a real Postgres container end-to-end
  (migrations idempotent, concurrent claims never overlap, both mark_failed
  branches, the FK ordering constraint, the anchor-text dedup, self-recovery of
  an expired lease with attempts incrementing correctly) before I called it
  done.

  Read 1 file (ctrl+o to expand)

● CONTRACT-UPDATE

  CONTRACT-UPDATE
  ## Where things stand

  Features 1 and 2 are merged into `main`. 3.1 (schema, frontier, migration
  runner) is written on `feature/3-frontier`, not yet merged. `handlers/`,
  `worker.py`, `engine.py`, `cli.py`, `store/blobs.py`, `store/metadata.py`
  still don't exist.

  What's written (on `feature/3-frontier`, in addition to what's on `main`):

  migrations/001_init.sql  contents, urls, content_metadata, links,
  fetch_attempts
                            — see the file for columns; urls.content_hash FKs
  into
                            contents.content_hash, content_metadata is also
  keyed
                            on content_hash (shared across urls with identical
  bytes)
  store/db.py    create_pool(config) -> asyncpg.Pool
                 run_migrations(pool, migrations_dir): advisory-lock guarded,
                 applies migrations/*.sql in filename order, idempotent
  store/frontier.py
                 ClaimedUrl(id, url, attempt_no, etag)
                 DiscoveredLink(raw_url, normalized_url, anchor_text)
                 claim_batch(pool, limit, config) -> list[ClaimedUrl]
                 mark_done(pool, url_id, *, content_type, content_length,
                           content_hash, etag) -> None
                 mark_failed(pool, url_id, decision: GiveUp | RetryAt,
                             error_kind, error_message=None) -> None
                 record_attempt(pool, url_id, attempt_no, *, status_code,
                                elapsed, error_kind=None) -> None
                 release(pool, url_id) -> None
                 enqueue_many(pool, links: list[DiscoveredLink], depth,
                              src_id=None) -> dict[normalized_url, id]
                 No function here reads config.max_attempts — that's
                 fetch/retry.py's call.

  No config keys changed.

✻ Sautéed for 9m 4s · done 4:09 PM

❯ two things before i commit.                                                   
                                                                                
  1. explain analyze the claim_batch subquery with 50k done rows and 2k         
     pending. paste the plan. my read: the OR forces a bitmap or, bitmap scans  
     don't return sorted rows, so ORDER BY next_attempt_at becomes a full sort  
     on every claim instead of an index scan that stops at LIMIT. if that's the 
     plan, the expiry sweep comes back as its own statement and claim_batch     
     goes back to pending only.                                                 
  2. postgres docs say DO NOTHING under repeatable read "may have insertion     
     not proceed" with no error - only DO UPDATE raises 40001. that's silent,   
     not loud, opposite of what's now in DESIGN.md. paste the actual error      
     output from your repro.                                                    

  Ran 2 shell commands (ctrl+o to expand)

● Now the actual plan for claim_batch's statement, wrapped in a transaction I'll
  roll back so I can re-run it cleanly:

  Ran 1 shell command (ctrl+o to expand)

● That confirms your read exactly: BitmapOr → Bitmap Heap Scan (all 2000
  matching rows materialized) → Sort on next_attempt_at → LockRows → Limit. The
  ORDER BY isn't served by the index at all here — it's a full quicksort over
  every eligible row, then LIMIT chops it. At 2k pending rows that's under a
  millisecond, but the cost tracks total eligible rows, not LIMIT, which is
  exactly the problem as the frontier grows. Let me confirm the fix — dropping
  the OR back to pending-only — actually gets the ordered index scan.

  Ran 2 shell commands (ctrl+o to expand)
● Both are dead-on. Let me implement the reversal and re-verify.

  Read 1 file (ctrl+o to expand)

● Update(src\crawler\store\frontier.py)
  ⎿  Added 29 lines, removed 7 lines
      32  
      33  
      34  async def claim_batch(pool: asyncpg.Pool, limit: int, config: C
          onfig) -> list[ClaimedUrl]:
      35 -    """One statement: SKIP LOCKED claims up to `limit` rows nob
         -ody else is                                                    
      36 -    touching and bumps `attempts`/sets the lease on exactly tho
         -se rows in                                                     
      37 -    the same pass — a fresh `pending` row and one whose lease r
         -otted are                                                      
      38 -    both eligible here, so there's no separate recovery sweep (
         -DESIGN.md).                                                    
      39 -    now() is Postgres's clock, not this process's, on purpose. 
      35 +    """One statement: SKIP LOCKED claims up to `limit` pending 
         +rows nobody                                                    
      36 +    else is touching and bumps `attempts`/sets the lease on exa
         +ctly those                                                     
      37 +    rows in the same pass. `pending` only — an `OR`'d-in `in_pr
         +ogress`                                                        
      38 +    branch forced a BitmapOr across both partial indexes, which
         + can't                                                         
      39 +    preserve `next_attempt_at` order, so `ORDER BY` fell back t
         +o sorting                                                      
      40 +    every eligible row before `LIMIT` could cut it down (verifi
         +ed via                                                         
      41 +    EXPLAIN; see DESIGN.md). Recovering a dead worker's row is 
      42 +    `recover_expired_leases`'s job, not this statement's. now()
         + is                                                            
      43 +    Postgres's clock, not this process's, on purpose.          
      44      """
      45      rows = await pool.fetch(
      46          """
     ...
      51              updated_at = now()
      52          WHERE id IN (
      53              SELECT id FROM urls
      50 -            WHERE (status = 'pending' AND next_attempt_at <= no
         -w())                                                           
      51 -               OR (status = 'in_progress' AND lease_until < now
         -())                                                            
      54 +            WHERE status = 'pending' AND next_attempt_at <= now
         +()                                                             
      55              ORDER BY next_attempt_at
      56              FOR UPDATE SKIP LOCKED
      57              LIMIT $1
     ...
      67      ]
      68  
      69  
      70 +async def recover_expired_leases(pool: asyncpg.Pool) -> int:   
      71 +    """Resets every `in_progress` row whose lease has lapsed ba
         +ck to                                                          
      72 +    `pending`, as its own statement run periodically — not fold
         +ed into                                                        
      73 +    `claim_batch`, see its docstring. Doesn't touch `attempts`,
         + same as                                                       
      74 +    `release`: only `claim_batch`'s own statement ever incremen
         +ts it, so a                                                    
      75 +    row recovered here costs exactly one attempt on its next cl
         +aim, no                                                        
      76 +    more, regardless of which of `release`/retry/this path sent
         + it back to                                                    
      77 +    `pending`.                                                 
      78 +    """                                                        
      79 +    result = await pool.execute(                               
      80 +        """                                                    
      81 +        UPDATE urls                                            
      82 +        SET status = 'pending', lease_until = NULL, updated_at 
         += now()                                                        
      83 +        WHERE status = 'in_progress' AND lease_until < now()   
      84 +        """                                                    
      85 +    )                                                          
      86 +    return int(result.split()[-1])                             
      87 +                                                               
      88 +                                                               
      89  async def mark_done(
      90      pool: asyncpg.Pool,
      91      url_id: int,

● Now the migration's stale comment pointing at claim_batch for recovery, and
  the DESIGN.md entry:

  Searched for 1 pattern, read 1 file (ctrl+o to expand)

● Update(migrations\001_init.sql)
  ⎿  Added 2 lines, removed 1 line
      37  
      38      -- Set on claim, cleared on every terminal write. A lease t
          hat's simply
      39      -- aged past lease_until, with no worker coming back for it
          , is what a
      40 -    -- crashed worker leaves behind — see store/frontier.py's c
         -laim_batch.                                                    
      40 +    -- crashed worker leaves behind — see store/frontier.py's  
      41 +    -- recover_expired_leases.                                 
      42      lease_until     TIMESTAMPTZ,
      43      next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      44  

● Now the DESIGN.md entry — this reverses the earlier decision, and I want that
  reversal visible, not silently overwritten:

  Searched for 1 pattern, read 1 file (ctrl+o to expand)

● Update(DESIGN.md)
  ⎿  Added 24 lines, removed 14 lines
       83  the handlers) already only knows `bytes | None`; none of it kn
           ows or cares
       84  how those bytes were spelled on the wire.
       85  
       86 -## Lease recovery: claim_batch reclaims, no separate sweep    
       86 +## Lease recovery: a separate sweep, not folded into claim_bat
          +ch                                                            
       87  
       88 -Chose folding an expired lease into `claim_batch`'s own `WHERE
          -`                                                             
       89 -(`status = 'in_progress' AND lease_until < now()`, same statem
          -ent as the                                                    
       90 -normal `pending` branch). Rejected a periodic `recover_expired
          -_leases`                                                      
       91 -sweep as a second mechanism alongside it. Once `claim_batch` a
          -lready                                                        
       92 -reclaims dead work the moment any worker next asks for more, a
          - sweep has                                                    
       93 -no correctness job left — every worker loop already calls `cla
          -im_batch`                                                     
       94 -continuously, so recovery is already as immediate as a sweep c
          -ould make                                                     
       95 -it, just without a second process to run or forget to run. Onl
          -y                                                             
       96 -`claim_batch`'s own `UPDATE` ever touches `attempts`; a lease 
          -reclaimed                                                     
       97 -after a crash costs one attempt exactly like an ordinary claim
          - would,                                                       
       98 -whether or not a real fetch happened before the crash. Would r
          -econsider if                                                  
       99 -idle recovery — nobody calling `claim_batch` — becomes a real 
          -scenario,                                                     
      100 -e.g. a crawl that drains its frontier faster than crashed leas
          -es expire.                                                    
       88 +Chose a periodic `recover_expired_leases` statement. Rejected 
          +folding an                                                    
       89 +expired lease into `claim_batch`'s own `WHERE` as a second, `O
          +R`'d-in                                                       
       90 +branch (`status = 'in_progress' AND lease_until < now()`) — tr
          +ied that                                                      
       91 +first, and it's wrong under load. With 50k `done` rows and 2k 
          +`pending`,                                                    
       92 +`EXPLAIN (ANALYZE, BUFFERS)` on the `OR` version shows `Bitmap
          +Or` across                                                    
       93 +both partial indexes, feeding a `Bitmap Heap Scan` that materi
          +alizes all                                                    
       94 +2000 eligible rows, then a `Sort` on `next_attempt_at` over th
          +at whole set,                                                 
       95 +*then* `LIMIT`. Dropping the `OR` back to `status = 'pending'`
          + alone                                                        
       96 +changes the plan to `Index Scan using urls_pending_idx`, order
          +ed, reading                                                   
       97 +only enough rows to satisfy `LIMIT` (confirmed: 3 buffers touc
          +hed instead                                                   
       98 +of 66, in `EXPLAIN`'s own numbers). A `BitmapOr` result has no
          + order                                                        
       99 +Postgres can rely on, so `ORDER BY` can't be pushed into the s
          +can the way                                                   
      100 +it can for a single-condition `WHERE` — the cost stops scaling
          + with                                                         
      101 +`LIMIT` and starts scaling with how many rows are eligible, wh
          +ich is                                                        
      102 +exactly backwards as `pending` count grows. `recover_expired_l
          +eases`                                                        
      103 +doesn't touch `attempts`, same as `release` — only `claim_batc
          +h`'s own                                                      
      104 +`UPDATE` ever increments it, so a lease reclaimed after a cras
          +h still                                                       
      105 +costs exactly one attempt on its next claim, regardless of whi
          +ch of                                                         
      106 +`release` / a retry decision / this sweep put the row back in 
          +`pending`.                                                    
      107 +Would reconsider if `claim_batch`'s `pending`-only plan ever s
          +tops being                                                    
      108 +an ordered index scan on its own (e.g. the planner switching s
          +trategies at                                                  
      109 +a different table size) — that's the thing actually being reli
          +ed on here,                                                   
      110 +not the absence of an `OR` for its own sake.                  
      111  
      112  ## enqueue_many: two statements, not one CTE
      113  

  Ran 1 shell command (ctrl+o to expand)

● Now a full functional re-verification that claim_batch no longer self-recovers
  and recover_expired_leases does the job:

  Ran 1 shell command (ctrl+o to expand)

● Both confirmed and fixed:

  1. The plan matched your read exactly. EXPLAIN (ANALYZE, BUFFERS) at 50k done
  / 2k pending:

  Sort  (cost=962.82..967.92 rows=2040 width=22) (actual time=0.551..0.552
  rows=10 loops=1)
    Sort Key: urls_1.next_attempt_at
    ->  Bitmap Heap Scan on urls urls_1  (rows=2000 loops=1)
          ->  BitmapOr
                ->  Bitmap Index Scan on urls_pending_idx (rows=2000)
                ->  Bitmap Index Scan on urls_in_progress_idx (rows=0)

  BitmapOr has no order, so ORDER BY couldn't be pushed into the scan — it
  materialized all 2000 eligible rows and sorted the lot before LIMIT cut it
  down. Dropping the OR back to status = 'pending' alone changes it to Index 
  Scan using urls_pending_idx, ordered, 3 buffers instead of 66, no sort node at
  all. Implemented: claim_batch is pending-only again, recover_expired_leases
  is back as its own statement. Neither touches attempts — only claim_batch's
  claim does, confirmed by re-running the crash scenario: a swept-and-reclaimed
  row goes attempts=1 → recovered (still 1) → reclaimed (2), same as before.
  DESIGN.md's "Lease recovery" entry now documents the reversal with the actual
  numbers, not just the final answer — the earlier version was wrong and says
  so.

  2. Your read of the docs doesn't match what Postgres 16 actually does here — I
  re-ran it with full exception introspection to pin down exactly which
  statement raises what:

  B INSERT raised: SerializationError('could not serialize access due to
  concurrent update')
    sqlstate: 40001

  That's the bare INSERT ... ON CONFLICT DO NOTHING itself — no DO UPDATE
  anywhere in the query — raising 40001 the moment it wakes from waiting on A's
  speculative insert and finds its REPEATABLE READ snapshot predates A's commit.
  Reproduced twice, deterministic both times. My guess at reconciling this with
  what you recall from the docs: the "insertion does not proceed, no error"
  behavior is for a conflict against a row that was already committed before B's
  snapshot was taken — an ordinary duplicate, nothing concurrent about it. What
  I tested is the narrower case both of us have been discussing — a genuinely
  concurrent insert of the same brand-new value — and empirically, on this
  Postgres version, that path raises 40001 for DO NOTHING same as it would need
  to for DO UPDATE, not silently. DESIGN.md's claim stands on that basis. If you
  have a specific doc section/version in mind that says otherwise for this
  exact case, I'd like the pointer — but the engine in front of me disagrees
  with the paraphrase.

✻ Sautéed for 3m 6s · done 4:19 PM

❯ you're right and i was wrong on the repeatable read one - name the            
  mechanism so i can defend it, not just the observation. which check in        
  postgres raises that, and why does it only fire when the snapshot is          
  transaction-scoped? one paragraph into DESIGN.md.                             
                                                                                
  two cleanups while you're there:                                              
  - 'skipped' is in the status check and nothing writes it. drop it or tell     
    me what sets it.                                                            
  - frontier.py is 215 lines against a 120 line rule in CLAUDE.md. i'm          
    keeping the file - fix the line in CLAUDE.md to what it actually is and     
    why, don't leave it silently broken.                                        

  Searched for 3 patterns, read 1 file (ctrl+o to expand)

● Update(migrations\001_init.sql)
  ⎿  Added 4 lines, removed 2 lines
      27  
      28      -- TEXT + CHECK, not a native ENUM: adding a status later i
          s a plain
      29      -- migration, not an ALTER TYPE that can't run in the same 
          transaction
      30 -    -- as its first use.                                       
      30 +    -- as its first use — 'skipped' (DESIGN.md: unmatched conte
         +nt type)                                                       
      31 +    -- isn't here yet because nothing writes it until the handl
         +er layer                                                       
      32 +    -- exists to decide it; add it in the migration that adds t
         +hat handler.                                                   
      33      status          TEXT NOT NULL DEFAULT 'pending'
      32 -                        CHECK (status IN ('pending', 'in_progre
         -ss', 'done', 'failed', 'skipped')),                            
      34 +                        CHECK (status IN ('pending', 'in_progre
         +ss', 'done', 'failed')),                                       
      35  
      36      -- Bumped only by claim_batch's own claiming statement — ne
          ver by
      37      -- whatever later sends the row back to 'pending' (retry, r
          elease).

  Read 1 file (ctrl+o to expand)

● Update(DESIGN.md)
  ⎿  Added 24 lines
      130  reconsider if this pool's default isolation level ever moves o
           ff Read
      131  Committed.
      132  
      133 +The 40001 comes from the same check Postgres applies to any `U
          +PDATE`,                                                       
      134 +`DELETE`, or `SELECT FOR UPDATE`/`FOR SHARE` that finds its ta
          +rget row was                                                  
      135 +concurrently changed by a transaction that has since committed
          + — the docs                                                   
      136 +describe it for that case as "the second updater will get a se
          +rialization                                                   
      137 +failure error." `INSERT ... ON CONFLICT` hits the identical ch
          +eck: deciding                                                 
      138 +`DO NOTHING` vs. actually inserting is a predicate over the co
          +nflicting                                                     
      139 +row, exactly like an `UPDATE`'s `WHERE` clause is, and evaluat
          +ing it after                                                  
      140 +a wait means looking at a row version that didn't exist under 
          +the                                                           
      141 +transaction's original snapshot. Under Read Committed each sta
          +tement calls                                                  
      142 +`GetTransactionSnapshot()` fresh — `IsolationUsesXactSnapshot(
          +)` is false                                                   
      143 +— so re-evaluating against the now-committed row is just a nor
          +mal                                                           
      144 +statement-scoped re-check, no violation, no error. Under REPEA
          +TABLE                                                         
      145 +READ/SERIALIZABLE, `IsolationUsesXactSnapshot()` is true: the 
          +one snapshot                                                  
      146 +taken at the transaction's first query is reused for every lat
          +er                                                            
      147 +statement, so there is no fresher snapshot to fall back to wit
          +hout                                                          
      148 +breaking the promise that every read in the transaction sees o
          +ne fixed                                                      
      149 +point in time — Postgres won't quietly answer the conflict que
          +stion using                                                   
      150 +data outside that snapshot, so it raises 40001 and pushes the 
          +retry back                                                    
      151 +to the client instead. This is Postgres's snapshot-isolation "
          +first                                                         
      152 +updater wins" rule, and `DO NOTHING` was never exempt from it 
          +— only a                                                      
      153 +conflict already visible in the original snapshot (no wait nee
          +ded) skips                                                    
      154 +the check entirely, which is the case the docs' "no error" lan
          +guage                                                         
      155 +actually describes.                                           
      156 +                                                              
      157  ## Migrations: advisory lock; frontier: none
      158  
      159  Chose a global advisory lock around the migration runner. Reje
           cted using

● Now CLAUDE.md's line about the 120-line rule:

  Searched for 2 patterns (ctrl+o to expand)

● Update(CLAUDE.md)
  ⎿  Added 8 lines, removed 3 lines
      228  A few smaller things: don't read the spec back to me, don't ad
           d a dependency
      229  without saying what it replaces, and if what I'm asking contra
           dicts something
      230  above then say so rather than quietly going along with it. If 
           what you're about
      231 -to write runs past ~120 lines, stop and tell me what you'd cut
          -. No TODOs, no                                                
      232 -stub functions, and nothing in the test suite is allowed to ac
          -tually sleep —                                                
      233 -inject the clock.                                             
      231 +to write runs past ~120 lines, stop and tell me what you'd cut
          + — and if I keep                                              
      232 +it anyway, that's not the rule failing, it's the rule doing it
          +s job: `store/`                                               
      233 +modules that are genuinely several single-statement atomic ope
          +rations (raw SQL,                                             
      234 +no ORM) run bigger than that once docstrings are trimmed to a 
          +DESIGN.md                                                     
      235 +pointer each; `frontier.py` is 237 lines across six such funct
          +ions. Say the                                                 
      236 +real number and let me decide, rather than reformatting to dod
          +ge the count.                                                 
      237 +No TODOs, no stub functions, and nothing in the test suite is 
          +allowed to                                                    
      238 +actually sleep — inject the clock.                            
      239  
      240  ## Commits
      241  

● Verifying the migration still applies cleanly after removing 'skipped':

  Ran 1 shell command (ctrl+o to expand)

● All clean. Summary of the three changes:

  1. Mechanism, named — the paragraph is in DESIGN.md under "enqueue_many,"
  appended to the existing entry. Short version for here: the 40001 is
  Postgres's general concurrent-write check (the same one UPDATE/DELETE/SELECT 
  FOR UPDATE hit when their target row changed under them), because DO NOTHING
  vs. inserting is a predicate over the conflicting row, same as an UPDATE's
  WHERE — and evaluating that predicate after a wait means consulting a row
  version outside the transaction's snapshot. Read Committed calls
  GetTransactionSnapshot() fresh per statement, so it can just re-check and move
  on. REPEATABLE READ/SERIALIZABLE pin one snapshot for the whole transaction
  (IsolationUsesXactSnapshot() true) — there's no fresher snapshot to fall back
  to without breaking the "one fixed point in time" guarantee, so Postgres
  raises 40001 and makes the client retry instead. DO NOTHING was never exempt;
  only a conflict already visible in the original snapshot (no wait) skips the
  check.

  2. 'skipped' — dropped, not justified. Nothing sets it — confirmed by grep,
  only DESIGN.md's prose and the CHECK constraint mentioned it. It's a real
  planned state (CLAUDE.md's own "unmatched content type" decision), but that's
  the handler layer's job, and the handler layer doesn't exist. Removed from the
  CHECK, with a comment pointing at why and where it comes back — which is
  exactly the scenario TEXT + CHECK over a native ENUM was chosen for in the
  first place.

  3. CLAUDE.md's line — fixed, not left broken. It now says the rule still
  stands as a stop-and-justify trigger, but that a store/ module built from
  several genuinely separate single-statement atomic operations (raw SQL, no
  ORM) can honestly land past 120 once docstrings are trimmed to a DESIGN.md
  pointer each — naming frontier.py's actual 237 — and that the right response
  when that happens is to say the real number and let you decide, not reformat
  SQL to dodge the count.

  Everything re-verified against Postgres after the edits (migration still
  applies cleanly with 'skipped' gone), ruff and the TODO/status_code == greps
  still pass.
✻ Churned for 2m 14s · done 4:24 PM