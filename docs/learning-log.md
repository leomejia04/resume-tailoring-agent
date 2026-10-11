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
