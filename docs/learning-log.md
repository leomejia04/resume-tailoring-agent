# Learning log

Short notes on concepts introduced while building, in plain language.

## Milestone 1: document proof

- **A DOCX is a ZIP of XML files.** `word/document.xml` holds the text;
  `styles.xml`, `numbering.xml` (bullets), and `theme` hold shared formatting.
  You can rename a `.docx` to `.zip` and look inside.
- **Paragraphs and runs.** A paragraph (`w:p`) has paragraph properties
  (`w:pPr`: bullet, indent, spacing, tab stops) and a list of runs (`w:r`). A run
  is a stretch of text sharing one formatting (`w:rPr`: font, size, bold).
  "Built **Python** pipelines" is three runs. Editing safely means changing runs
  while leaving `w:pPr` alone.
- **Why not just `paragraph.text = "..."`?** python-docx's setter drops all runs
  and creates one run with no formatting, so bolding and explicit fonts vanish.
- **Canonical XML (C14N)** is a standard way to serialize XML so two documents
  with the same meaning produce identical bytes. We compare canonical forms so
  harmless re-serialization does not look like a change.
- **Optimistic concurrency, in miniature.** The edit carries the text the caller
  last saw. If the document changed in between, the edit is rejected rather than
  applied to the wrong content. The same idea will protect approvals later
  (version numbers instead of text).
- **Subprocess safety.** Passing a list of arguments with `shell=False` means no
  shell parses the command, so a filename like `a.docx; rm -rf /` is just a
  strange filename, not a command. We go further and never pass user filenames.
- **Metric-compatible fonts.** Liberation Sans has the same character widths as
  Arial, so text wraps identically on Windows and in a Linux container. Font
  width differences are a common cause of "fits on one page locally, two pages
  in production".
- **Inspecting the real PDF.** We check the artifact users download, not the
  DOCX we intended: page count, extractable text (ATS parsers need it), text
  touching page edges (clipping), and phrase order (reading order).
- **Docker image vs container.** An image is a packaged, read-only environment
  ("Debian + LibreOffice + fonts"); a container is one running copy of it. We
  start a fresh container for every conversion and delete it afterwards, so
  nothing from one user's document can linger for the next.
- **Sandboxing a converter.** Office files are a classic attack vector, so the
  container gets no network, no access to your folders, a read-only system, no
  root user, and memory/CPU/process limits. Data goes in through stdin and the
  PDF comes out through stdout, like piping in a terminal: `cat a.docx | convert`.
- **Pinning by digest.** `debian:trixie-slim` is a moving tag; `@sha256:...` is
  a fingerprint of exact image contents, so builds are reproducible.
- **Line endings.** Windows uses CRLF, Linux LF. A CRLF in a shell script makes
  Linux look for `/bin/sh\r` (with a hidden carriage return), which does not exist; `.gitattributes` forces LF.
- **Tests that catch the tester.** The reading-order check failed the first
  real run because the test listed phrases in the wrong order. A check that
  can fail is worth more than one that always passes.

## Milestone 2: frontend/backend connection

- **An API call is a function call over HTTP.** `previewJobInput(job)` in
  TypeScript becomes `POST /api/v1/jobs/preview` with a JSON body; FastAPI
  turns the JSON into a Pydantic object, runs the Python function, and turns
  its return value back into JSON. Status codes say how it went: 200 OK,
  413 too large, 422 invalid input, 500 our fault.
- **Origins and CORS.** An origin is scheme + host + port. Browsers stop a page
  from one origin reading responses from another unless the server opts in
  with CORS headers. CORS protects users' browsers, not the server: curl
  ignores it, which is why the server still validates everything.
- **The dev proxy.** Vite forwards `/api/*` to FastAPI, so the browser sees a
  single origin and no CORS is needed. Production should mirror this.
- **Never trust the client.** Frontend validation gives fast feedback;
  Pydantic on the server is the real check, because anyone can send requests
  without our UI.
- **Request IDs** connect what a user sees ("reference ab12…") to the exact
  server log line, without showing users internal details.
- **ASGI middleware** wraps the app like layers of an onion: each request
  passes through them in order and each response passes back out.
- **React StrictMode** runs effects twice in development to expose missing
  cleanup; seeing one cancelled health request followed by a successful one is
  that check working.
- **Picking versions.** The newest major version is not always usable:
  TypeScript 7 was out, but typescript-eslint supports TypeScript below 6.1,
  so we pinned 6.0.3. Peer-dependency ranges tell you what works together.

## Milestone 3: accounts and private data

- **Authentication vs authorization.** Authentication proves who you are
  (password, then a signed token). Authorization decides what you may touch
  (only rows whose `owner_id` is you). Most data leaks are authorization bugs.
- **JWTs.** A signed JSON token: anyone can read it, nobody can change it
  without the private key. We verify the signature with the public key (JWKS)
  and check who issued it, who it is for, and when it expires.
- **HttpOnly cookies.** JavaScript cannot read them, so an injected script
  cannot steal the session. The browser attaches them automatically, which is
  why CSRF protection is needed.
- **CSRF, double-submit.** A malicious site can make your browser send our
  cookies, but it cannot read our readable CSRF cookie or set our custom
  header. Requiring header == cookie (plus a trusted Origin) blocks forgery.
- **Migrations.** Alembic scripts are version control for the database
  schema. `alembic upgrade head` applies them; `alembic check` proves the
  Python models still match the real database.
- **Row-level security.** A rule attached to a table that filters every query.
  Even a buggy `SELECT * FROM app.master_versions` returns only your rows. It
  only protects you if the connecting role cannot bypass it, which is why the
  backend uses `app_backend` and never the service role.
- **Transaction-local settings.** `set_config(..., true)` lasts only until the
  transaction ends, so a pooled connection never carries the last user's ID.
- **Least privilege.** Grants decide which commands a role may run at all;
  RLS decides which rows. No UPDATE/DELETE grant on history means even our own
  code cannot rewrite a master or a fact version.
- **Optimistic concurrency.** Each edit says "I am changing version 3". If
  another tab already made version 4, the database refuses and the user
  reloads instead of silently overwriting.
- **Testing what the browser does.** jsdom kept focus on a disabled button;
  a real browser dropped it. Checking the real page found an accessibility
  bug the unit tests could not.

## Milestone 4: structured AI baseline

- **Verify before integrating.** We checked the model's official page for its
  exact ID, Structured Outputs support, reasoning settings, and prices instead
  of trusting memory or search snippets (which disagreed on price).
- **Structured outputs.** The model must answer in a JSON shape defined by a
  Pydantic class. That guarantees the shape, not the truth, so every result is
  validated again by our code.
- **Grounding with citations.** Facts get short aliases (E1, E2). Each bullet
  must cite them, and Python checks that cited facts are real, confirmed, from
  the same job, and actually contain every number, year, and tool the bullet
  mentions. The model can phrase; it cannot invent.
- **Prompt injection.** Text inside a job posting can try to give the model
  orders. We cannot fully prevent a model from obeying, so we make obeying
  useless: unsupported claims are removed by deterministic checks.
- **Short transactions around slow calls.** Never keep a database transaction
  open while waiting on a network call; record intent, commit, call, then
  record the result.
- **Atomic budgets.** "Check then spend" can overspend under concurrency.
  A single conditional UPDATE (`spent + reserved + cost <= cap`) reserves money
  safely because the row lock makes concurrent requests take turns.
- **Decimals for money.** Floating point cannot represent 0.1 exactly; costs
  use Python `Decimal` and PostgreSQL `numeric`.

## Milestone 5: review and export

- **Immutable versions make undo trivial.** Nothing is overwritten, so "undo"
  is just "make a new version that copies an old one", and history is never
  lost.
- **Bind approval to bytes.** A SHA-256 hash is a fingerprint of the exact PDF.
  Approving "version 4 with hash ab12..." means any change, even one byte,
  is detectably not what was approved.
- **Never hide what was cut.** Limits and one-page fitting remove bullets; the
  report lists every removal so the user sees exactly what is not on the page.
- **Check the artifact, not the intent.** The reading-order check on the real
  PDF caught a duplicated degree line that page-count checks could not.
- **CSS grid and long words.** A grid column grows to fit its longest
  unbreakable word (like a long filename); `grid-cols-1` (minmax(0, 1fr)) and
  `break-all` let it shrink on phones.
- **Framing as a permission.** The API forbids being embedded by default;
  the PDF preview route alone allows framing by our own origin.

## Milestone 6: ATS reports and reliability

- **A rubric is a contract.** Writing the weights, credits, and edge cases
  down (ADR 0006) before showing a number means every score can be explained
  requirement by requirement and reproduced later with the same version.
- **Exact fractions for scoring.** `Fraction(50, 65)` stays exact through
  averaging and weighting; rounding happens once, at display time, so two
  equal inputs can never round to different results.
- **Regex word boundaries.** `(?<![A-Za-z0-9])Java(?![A-Za-z0-9])` is a
  lookbehind and a lookahead: "Java" must not touch a letter or digit on
  either side, so "JavaScript" does not match. Short names like R or C need
  stricter neighbours to avoid R&D or C++.
- **Unknown is not zero, and not free.** Requirements we cannot check stay in
  the denominator (so the estimate cannot rise by being unsure) but are shown
  as "uncertain", not "missing". A report with nothing to score shows no
  number at all.
- **Shared test state leaks.** A test that reserved budget without releasing
  it slowly used up the application-wide daily cap until unrelated runs were
  refused. Tests that touch shared counters must clean up after themselves.
- **Store facts, derive status.** A report for draft version 3 is true forever;
  what changes is whether version 3 is still current. Storing `stale` would
  need an UPDATE every time anything changed (and could be missed). Instead
  the row stays immutable and "stale" is computed when reading, by comparing
  the report's subject and rubric version with the run's current ones.
- **One transaction, one truth.** The draft version and its report are written
  in the same transaction (`flush()` gives the version its ID first). If
  either fails, neither exists, so the UI can never show a version whose
  score belongs to something else.
- **Partial unique indexes.** `CREATE UNIQUE INDEX ... WHERE status =
  'complete'` allows many failed attempts but only one complete report per
  subject. Paired with `INSERT ... ON CONFLICT DO NOTHING`, two tabs checking
  at once both succeed and only one row is kept.
- **Why the re-check is a POST.** Reading a run (GET) must never change data,
  so a missing report is shown as "not checked yet" rather than computed on
  the fly; the user (or, later, the worker) triggers the check explicitly.

### 6c: durable jobs

- **A queue can be a table.** A job is a row with a status. `SELECT ...
  FOR UPDATE SKIP LOCKED LIMIT 1` lets several workers ask for "the next
  job" at once: each locks a different row and skips the ones others hold,
  so no job runs twice and nobody waits.
- **Leases, not locks, survive crashes.** A worker that claims a job writes
  "mine until 12:01" and keeps extending it (heartbeats). If the process
  dies, the lease simply runs out and another worker retries the job. A
  database lock would have vanished with the connection, but so would the
  record of what was happening.
- **Fencing.** Every update a worker makes says `WHERE locked_by = me`. If
  its lease expired and another worker took over, the late worker's writes
  match no row and are ignored instead of overwriting the newer result.
- **Retry only what can change.** A timeout or a busy service may succeed a
  few seconds later, so it is retried with growing waits (backoff). A refusal,
  a spending limit, or a version conflict will fail the same way again, so it
  fails at once with a clear message.
- **Signed URLs are capabilities.** Whoever holds one can read (or create)
  that one file until it expires, with no password. That makes them perfect
  for handing a worker exactly the access one job needs, and dangerous to
  log: request logging stays off and the URLs are deleted when the job ends.
- **SECURITY DEFINER, carefully.** A function can run with its owner's
  privileges instead of the caller's. Owned by a tiny role (`app_queue`) that
  can only touch the queue, with a pinned `search_path`, it gives the worker
  one narrow cross-user door instead of a master key. A surprise on the way:
  after changing a function's owner, REVOKE statements from the previous
  owner are silently ignored, so privileges must be set first.
- **202 Accepted.** "I have your request and will do it" is a different
  promise from "here is the result" (200/201). The client then polls the
  resource until the work shows up, which also means a page reload or a
  second tab shows the same truth as the first.

### 6d: bounded automatic improvement

- **Let the model propose, let code decide.** The model only suggests a
  rewrite; Python checks every claim against confirmed facts and re-scores
  the result with the same rubric. A change is kept only if it helps, so the
  model cannot argue its way into a better number.
- **Score both sides with one ruler.** To find gaps worth fixing, the facts
  are scored exactly like the draft (same term, negation, and context rules).
  "The facts would earn more credit here" is then a precise, testable
  statement, not a guess.
- **Pareto improvement.** A trade (gain Tableau, lose SQL) can leave the
  total unchanged or even raise it while making the resume worse for one
  requirement. Requiring "some gain, no loss" rules that out.
- **Caps belong in the database.** A counter in code can be bypassed by a
  retry, a restart, or two workers. `CHECK (attempt BETWEEN 1 AND 2)` plus a
  unique (run, attempt) pair cannot: the third insert fails whoever tries it.
  Claim the slot first, then spend the money.
- **Chain jobs in the same transaction.** Marking the draft job succeeded and
  queuing the improvement in one commit means there is no moment where one
  happened and the other was lost.


### 6e: manual application tracker

- **Let the schema refuse what must never happen.** The tracker links an
  entry to an approval with a composite foreign key on (approval, owner).
  Even a buggy query that forgot the owner check cannot point Alice's entry
  at Bob's resume: the database has no matching row to accept. The API still
  checks first, so the user sees "Choose one of your approved resumes"
  rather than a database error.
- **Optimistic concurrency in one statement.** `UPDATE ... WHERE id = :id AND
  lock_version = :seen` either changes exactly one row or none. "None" means
  someone else changed it since you loaded it (409) or it is gone (404). No
  locks are held while the user types, and no tab silently wins.
- **Count-then-insert needs a lock.** "Refuse after 1000 entries" is two steps
  (count, then insert), and two requests can both count 999. A transaction
  advisory lock per owner makes the steps happen one request at a time. The
  test proves it the honest way: with the lock removed, it fails.
- **Accessible names are computed, not seen.** A visually hidden
  `<span> Data Analyst</span>` after "Edit" produced the name
  "EditData Analyst": the leading space was dropped. An explicit `aria-label`
  that starts with the visible word ("Edit Data Analyst at ...") is
  unambiguous for screen readers and voice control.
- **Effects that change the URL can re-trigger themselves.** Removing a query
  parameter inside a data-loading effect changed the router's setter, re-ran
  the effect, and reloaded the form. The fix: load once, and change the URL
  from an event handler (when the form closes).
- **Dates without times.** `new Date('2026-10-01')` means midnight UTC, which
  is still September 30 in the Americas. A calendar date is shown by building
  it from its parts in local time.

## Milestone 7: measured retrieval

- **An embedding is a list of numbers that places a sentence by meaning.**
  `all-MiniLM-L6-v2` turns any text into 384 numbers. Sentences with similar
  meaning land near each other even without shared words ("vendor risk
  assessments" near "rated each supplier's risk").
- **Cosine similarity and normalization.** Similarity is the angle between
  two vectors. If every vector is scaled to length 1 ("L2-normalized"), the
  cosine is just the dot product, which is cheap and comparable across
  queries.
- **Full-text search is not `LIKE '%sql%'`.** Postgres splits text into
  words, stems them ("reporting" -> "report"), drops stop words, and ranks
  by how often and how close the query words occur. It finds word variants;
  embeddings find meaning; the exact matcher finds precise terms. Each
  misses things the others catch.
- **Reciprocal Rank Fusion.** Scores from different searches are not on the
  same scale, but ranks are. RRF gives each fact `1 / (60 + rank)` from each
  list and adds them up, so a fact that is near the top of several lists
  wins without any tuning.
- **Exact vs approximate vector search.** An exact search compares the query
  with every row. Approximate indexes (HNSW, IVFFlat) skip most rows to be
  fast on millions of vectors, at the cost of sometimes missing the best
  match. One user's few hundred facts do not need that trade.
- **Precision and recall at k.** Recall@5: of the facts a person would pick,
  how many are in the top 5? Precision@3: of the top 3, how many are right?
  For building a resume, missing a supporting fact (low recall) is worse
  than reading an extra one, which is why the selection keeps several facts
  per requirement plus fallbacks.
- **Measure the baseline first.** "RAG" is not automatically better. Sending
  every fact is complete by definition; retrieval only earns its place where
  the profile is too big to send, or where it measurably saves cost without
  losing evidence.
- **Pin and verify downloaded models.** A model name on a hub can change
  under you. A fixed revision plus a SHA-256 for every file means the bytes
  you tested are the bytes you run, and `trust_remote_code=False` means
  loading the model never runs code from the download.
