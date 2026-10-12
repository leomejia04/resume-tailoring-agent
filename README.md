# Resume Tailoring Agent

A multi-user web application that turns one master resume into truthful,
tailored, one-page PDF resumes for specific job postings. It reads your
resume, builds a bank of verified facts about your experience, analyzes a job
posting, drafts a tailored resume in which every line points back to a fact
you confirmed, checks it, renders the real PDF, and lets you review, edit,
approve, and download it.

The project runs locally. It was built as a complete, security-conscious
full-stack and AI engineering exercise; the full specification and build
history live in [CLAUDE.md](CLAUDE.md), and each major design choice has an
architecture decision record in [docs/decisions/](docs/decisions/).

## Contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Technologies](#technologies)
- [Engineering methods](#engineering-methods)
- [Using the app](#using-the-app)
- [Setup (Windows)](#setup-windows)
- [Running locally](#running-locally)
- [Checks](#checks)
- [Configuration](#configuration)
- [Repository layout](#repository-layout)
- [Updating dependencies](#updating-dependencies)

## What it does

- **Truthful tailoring.** The AI may reword, condense, emphasize, and reorder
  facts you have confirmed. It may not invent skills, tools, numbers, dates,
  titles, employers, or credentials. When a posting asks for something your
  profile does not show, the app asks you a precise question instead of
  guessing.
- **Your master stays untouched.** Uploading a new master creates a new
  version; every draft remembers which version it came from.
- **One page, real PDF.** Resumes are rendered to an actual PDF, checked for
  page count, clipping, reading order, and selectable text, and shown to you
  before you approve. If the content cannot fit within the configured limits,
  you get an actionable failure, never a silently shrunk font.
- **ATS match estimate.** A 0-100 estimate from a documented rubric, shown for
  your master (baseline), every draft, and the exact final PDF, with the
  evidence behind each requirement and any must-have gaps. It is an estimate,
  not an employer's actual score.
- **Review tools.** Direct edits, section rewrites, chat revisions, version
  history with compare and restore, formatting preferences, and up to two
  automatic, fact-checked improvement attempts per run.
- **Approval and download.** Approval binds to one exact version and the hash
  of its PDF; any later change requires a new approval. Downloads are named
  `FirstLast_Position_Company_CV.pdf`.
- **Application tracker.** A private list of where you applied (company,
  role, link, date, status, notes), optionally linked to the exact resume you
  sent.
- **Private by design.** Every account's data is isolated at the API, the
  database (row-level security), and file storage. Users can delete their
  account and everything derived from it.

## How it works

```
 Browser (React)
     │  JSON over HTTPS-style same-origin requests, HttpOnly session cookies
     ▼
 FastAPI API ──── validates input, checks who you are and what you own
     │
     ├── PostgreSQL (Supabase) ── profiles, fact versions, jobs, runs, drafts,
     │        + pgvector             reports, approvals, tracker; row-level security
     ├── Private file storage ──── master uploads, rendered PDFs
     └── Job queue (a Postgres table)
              │
              ▼
        Background worker ── analysis, drafting, rewrites, rendering, ATS checks
              │
              ├── OpenAI Responses API (schema-constrained output)
              ├── Local embedding model (retrieval)
              └── LibreOffice in a sandboxed container (DOCX → PDF)
```

1. **Upload a master resume** (PDF or DOCX). The server validates the file
   (size, type, signature), stores it privately, and extracts entries, dates,
   bullets, and skills into candidate facts.
2. **Verify facts.** You confirm, edit, or reject each fact in the experience
   bank. Only confirmed facts can ever appear in a resume. Each fact keeps a
   stable ID, its source text, context (professional, internship, academic,
   personal), and version history.
3. **Analyze a job posting.** Paste the posting text. The model
   returns a structured analysis (required and preferred qualifications,
   responsibilities, terminology), validated by Pydantic schemas and cached
   per posting version.
4. **Choose context.** If your verified profile fits the drafting budget, the
   whole profile is sent. Larger profiles use hybrid retrieval: exact matching,
   Postgres full-text search, and semantic search over local embeddings,
   combined with reciprocal rank fusion, per job requirement.
5. **Draft.** The model writes a structured draft in which each bullet cites
   fact IDs. Python then checks every claim deterministically: the cited facts
   must exist, belong to you, be confirmed, and actually support the wording.
   Unsupported numbers, tools, dates, and titles are removed; official job
   titles, employers, and dates are copied from your facts, never written by
   the model.
6. **Render and check.** The draft is rendered into a DOCX layout and
   converted to PDF by LibreOffice, then inspected: one page, no clipping,
   expected reading order, contact fields present, text selectable.
7. **Score.** The ATS estimate is computed in Python from validated
   requirement-to-evidence mappings (required 50%, responsibilities 25%,
   preferred 15%, terminology 10%), with rules against substring,
   negation, and keyword-repetition false matches.
8. **Improve, review, approve.** The worker may try up to two improvements
   that are kept only if the score rises without losing other matches. You
   review, edit, or chat; every change is re-checked and invalidates earlier
   approval. You approve the exact PDF and download it.

Long-running work (analysis, drafting, rendering, scoring) runs in a durable
background worker backed by a Postgres job table, so jobs survive restarts,
retry transient failures a bounded number of times, and can be cancelled.
Every model call reserves budget first against per-user and app-wide daily
spending limits.

## Technologies

| Area | Tools |
| --- | --- |
| Frontend | React 19, TypeScript, Vite, Tailwind CSS, React Router |
| Backend | Python 3.13, FastAPI, Pydantic, SQLAlchemy, Alembic |
| Data and auth | Supabase (Auth, PostgreSQL, private Storage), row-level security, pgvector |
| AI | OpenAI Responses API with schema-constrained output (`gpt-5.6-luna`, configurable); a free deterministic `demo` provider for local use |
| Retrieval | Sentence Transformers `all-MiniLM-L6-v2` (pinned revision, SHA-256 checked, CPU), Postgres full-text search, reciprocal rank fusion |
| Documents | python-docx, LibreOffice (in a Docker container), pdfplumber, pypdfium2 |
| Testing and quality | pytest, Vitest, React Testing Library, Ruff, ESLint, TypeScript checks |
| Tooling | Docker Desktop, Supabase CLI, pip-tools hash-locked requirements, npm lockfiles |

## Engineering methods

- **Grounded generation with provenance.** Every generated claim maps to
  evidence IDs. Validation is deterministic Python, not trust in the model or
  in vector similarity. Generated resumes never become evidence.
- **Measure before adding complexity.** Retrieval was built only after a
  full-profile baseline, then compared on a labeled evaluation set across Data,
  GRC, Business Analyst, and Tech Sales roles (precision/recall at k,
  coverage, factual errors, cost). Hybrid retrieval ranked best, but sending
  the whole profile kept more relevant facts for normal-sized profiles, so the
  app uses full context by default and retrieval only for large profiles
  ([ADR 0010](docs/decisions/0010-measured-retrieval.md)).
- **Schema-constrained AI output.** Model responses are parsed into Pydantic
  models with length, enum, and nesting limits; uploads, postings, and
  retrieved text are treated as untrusted data that cannot change
  instructions.
- **Defense in depth.** Authentication and per-resource authorization on every
  endpoint, a least-privilege database role with forced row-level security,
  CSRF and trusted-origin checks, rate limits, content security policy,
  upload signature checks, sandboxed document conversion without network
  access, and safe error responses with request IDs.
- **Immutable versions and optimistic concurrency.** Masters, facts, drafts,
  rendered artifacts, and reports are versioned; stale browser tabs cannot
  approve or overwrite newer work.
- **Bounded cost and reliability.** Atomic budget reservations, per-user job
  limits, idempotent actions, bounded retries and repair loops, and crash
  recovery through job leases.
- **Small, documented milestones.** Work was delivered in seven milestones,
  each with tests, an ADR, and notes in the
  [learning log](docs/learning-log.md).

## Using the app

1. **Create an account** on the sign-up page and sign in.
2. **Profile:** set your name prefix for file names and upload your master
   resume (PDF or DOCX).
3. **Experience:** review the extracted facts. Confirm what is accurate, edit
   what needs context or the right kind (professional, internship, academic,
   personal), and reject what is wrong.
4. **Job posting:** paste the posting text (plus its title, company, and an
   optional link, which is stored but never fetched) and start a run. The app
   analyzes it and may ask questions about requirements your confirmed facts
   do not cover; answer only what is true. Answers you confirm become new
   facts in your experience bank, with their context recorded.
5. **Review the draft** on the run page:
   - compare it with your master and see which facts were left out to fit one
     page;
   - edit bullets directly, rewrite a section, or ask for changes in chat;
   - browse, compare, and restore earlier versions;
   - adjust formatting (layout, bullets per entry and per project, bold
     project technologies, languages on their own line, locked entries);
   - read the ATS estimate, its evidence, and any must-have gaps.
6. **Render and approve:** view the real PDF, wait for the final checks to
   pass, approve that exact version, and download it.
7. **Tracker:** record where you applied and link the approved resume you
   sent.
8. **Dashboard** lists your runs and approved resumes; **Settings** holds
   default formatting preferences and account deletion.

Without an OpenAI key, set `LLM_PROVIDER=demo` in `.env` to try the full flow
with deterministic rules (clearly labeled as not AI). With `OPENAI_API_KEY`
set, the configured OpenAI model is used and costs are capped by the daily
budget settings. Resume and posting text is sent to OpenAI only in that mode.

## Setup (Windows)

Prerequisites:

- Python 3.12+ (developed on 3.13)
- Node.js 24 LTS (`frontend/package.json` requires >= 24.15)
- Docker Desktop with the WSL 2 engine (runs LibreOffice and the local
  Supabase stack; LibreOffice does not need to be installed on Windows)
- Git

Commands below were run in Git Bash. In PowerShell, use backslashes
(`.venv\Scripts\python`).

Build the converter image (repository root):

```bash
docker build -t resume-converter:dev docker/converter
```

Create the backend environment (from `backend/`):

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install --require-hashes -r requirements.txt -r requirements-dev.txt
```

Download the pinned embedding model (about 90 MB, into git-ignored
`backend/.models/`; each file is checked against a SHA-256). Without it,
drafting still works using the full verified profile:

```bash
.venv/Scripts/python -m scripts.fetch_embedding_model
```

Install frontend dependencies (from `frontend/`):

```bash
npm ci
```

### Local Supabase (accounts, database, storage)

Install the pinned Supabase CLI (repository root):

```bash
npm ci
```

Create a local JWT signing key (repository root; the file is git-ignored):

```bash
echo "[]" > supabase/signing_keys.json
```

```bash
npx supabase gen signing-key --algorithm ES256 --append
```

Start the stack (the first run downloads several container images):

```bash
npx supabase start
```

Write `.env` in the repository root from the running stack (from `backend/`;
generates a random password for the `app_backend` database role and refuses
to overwrite an existing `.env`):

```bash
npx --prefix .. supabase status -o env | .venv/Scripts/python -m scripts.local_env
```

Apply database migrations (from `backend/`):

```bash
.venv/Scripts/alembic upgrade head
```

Stop the stack when finished (data is kept for the next start):

```bash
npx supabase stop
```

While the stack runs: Studio (database browser) at http://127.0.0.1:54323
and Mailpit (captured emails such as password resets) at
http://127.0.0.1:54324.

## Running locally

Start the API (from `backend/`):

```bash
.venv/Scripts/python -m uvicorn app.main:create_app --factory --reload --port 8000 --no-access-log --no-server-header
```

Start the background worker in a second terminal (from `backend/`). Without
it, analysis, drafting, rendering, and checks stay "Waiting to start":

```bash
.venv/Scripts/python -m app.worker
```

Start the frontend in a third terminal (from `frontend/`):

```bash
npm run dev
```

Open http://127.0.0.1:5173. Vite forwards `/api` to the API on port 8000, so
the browser uses a single origin and no CORS configuration is needed. API
docs (development only): http://127.0.0.1:8000/api/docs

To serve the built frontend from the API on one origin instead: run
`npm run build` in `frontend/`, then start the API with
`FRONTEND_DIST=frontend/dist` (the `site-same-origin` entry in
`.claude/launch.json` does this on port 8002 with demo AI).

Windows tips:

- Stop servers with Ctrl+C. With `--reload`, force-killing only the watcher
  leaves its child process holding port 8000.
- If the worker is killed mid-job, the next worker retries that job once its
  60-second lease expires.
- If HTTPS calls to OpenAI fail with a certificate error, antivirus HTTPS
  scanning (for example Norton) is re-signing traffic: set
  `USE_OS_TRUST_STORE=true`.
- Stop `npm run dev` before `npm ci`; Windows locks loaded native modules.

## Checks

Backend (from `backend/`):

```bash
.venv/Scripts/python -m pytest -q
```

```bash
.venv/Scripts/ruff check .
```

```bash
.venv/Scripts/ruff format --check .
```

```bash
.venv/Scripts/alembic check
```

Integration tests run against the local Supabase stack and `.env` and are
skipped when they are unavailable; PDF render tests need Docker and the
converter image. Do not run a worker while the suite runs (it would claim
test jobs).

Frontend (from `frontend/`):

```bash
npm run typecheck
```

```bash
npm run lint
```

```bash
npm test
```

```bash
npm run build
```

Other scripts (from `backend/`; output goes to git-ignored `backend/out/`):

```bash
.venv/Scripts/python -m scripts.document_proof
```

```bash
.venv/Scripts/python -m scripts.eval_retrieval
```

```bash
.venv/Scripts/python -m scripts.smoke_test --base-url http://127.0.0.1:8002
```

- `document_proof` edits a fictional master DOCX, renders it, and inspects the PDF.
- `eval_retrieval` runs the labeled retrieval evaluation (local stack and model, no paid calls).
- `smoke_test` walks the whole flow over HTTP against a running same-origin instance.
- `eval_drafts --budget-usd 1.00` compares full-profile and retrieval drafts
  with live model calls. It is **paid**, refuses to run without a cap, and
  `--demo` checks it for free.

## Configuration

Environment variables (see [.env.example](.env.example); secrets belong only
in the git-ignored `.env`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_ENV` | `development` | `production` disables API docs and enables stricter settings and HSTS |
| `CORS_ALLOWED_ORIGINS` | (empty) | Exact origins allowed cross-origin; empty means same-origin only |
| `MAX_REQUEST_BYTES` | `262144` | Maximum request body size |
| `CONVERTER` | `auto` | `auto` uses a local `soffice` if found, else Docker; or `local` / `docker` |
| `CONVERTER_IMAGE` | `resume-converter:dev` | Docker image used by the `docker` converter |
| `SOFFICE_PATH` | (unset) | Path to a local `soffice` executable |
| `CONVERSION_TIMEOUT_SECONDS` | `60` | Hard limit per conversion |
| `SUPABASE_URL` | `http://127.0.0.1:54321` | Supabase project URL |
| `SUPABASE_ANON_KEY` | (unset) | Publishable key; auth and storage are unavailable without it |
| `SUPABASE_JWT_ISSUER` | derived | Override for the token issuer when the API reaches Supabase under another host name; refused in production |
| `DATABASE_URL` | (unset) | Runtime connection as the least-privilege `app_backend` role |
| `MIGRATION_DATABASE_URL` | (unset) | Schema-owner connection, used only by Alembic |
| `APP_DB_PASSWORD` | (unset) | Password the migration sets for `app_backend` |
| `TRUSTED_ORIGINS` | Vite dev origins | Origins allowed to send state-changing requests |
| `COOKIE_SECURE` | auto | Secure cookies in production, plain on localhost |
| `MAX_UPLOAD_BYTES` | `5242880` | Maximum master upload size |
| `LLM_PROVIDER` | auto | Empty: OpenAI if a key is set, else AI off; `demo` for local testing |
| `OPENAI_API_KEY` | (unset) | Server-side OpenAI key |
| `OPENAI_MODEL` | `gpt-5.6-luna` | Model used for analysis and drafting |
| `OPENAI_REASONING_EFFORT` | `low` | Reasoning effort sent with each call |
| `USE_OS_TRUST_STORE` | `false` | `true` verifies HTTPS with the operating system's certificates; verification stays on |
| `USER_DAILY_BUDGET_USD` / `APP_DAILY_BUDGET_USD` | `0.50` / `5.00` | Atomic spending limits checked before every model call |
| `RESERVATION_TTL_SECONDS` | `900` | An unsettled reservation (crash mid-call) is returned after this |
| `JOB_RUNNER` | `worker` | `worker`: `python -m app.worker` runs jobs; `inline`: the request runs its own job (tests and demos) |
| `JOB_MAX_ATTEMPTS` / `JOB_RETRY_BACKOFF_SECONDS` | `3` / `5` | Bounded retries for transient failures (backoff doubles) |
| `JOB_LEASE_SECONDS` | `60` | How long a silent worker keeps a job before another may retry it |
| `WORKER_CONCURRENCY` / `WORKER_POLL_SECONDS` | `2` / `1.0` | Worker threads and idle polling interval |
| `MAX_ACTIVE_JOBS_PER_USER` / `MAX_RUNNING_JOBS_PER_USER` | `4` / `1` | Per-user queue limit and fair-share running limit |
| `GRANT_TTL_SECONDS` | `3600` | Lifetime of signed file URLs handed to a job |
| `AUTO_IMPROVE` | `true` | Queue bounded automatic improvement after each draft (at most 2 attempts per run) |
| `MAX_APPLICATIONS_PER_USER` | `1000` | Tracker entries per user |
| `RETRIEVAL_MODE` | `auto` | `auto`: full profile when it fits the budget, else hybrid retrieval; `full`; `hybrid` |
| `RETRIEVAL_FULL_CONTEXT_MAX_CHARS` | `60000` | In `auto`, profiles up to this size are sent whole |
| `RETRIEVAL_TOP_K` | `4` | Facts kept per job requirement by hybrid retrieval |
| `EMBEDDING_MODEL_DIR` | `backend/.models/all-MiniLM-L6-v2` | Where the pinned model snapshot lives |
| `EMBEDDING_THREADS` | `2` | CPU threads for embeddings |
| `PRICE_*_PER_MTOK` | official rates | Used for cost accounting |
| `FRONTEND_DIST` | (unset) | Built frontend folder the API serves on the same origin; must contain `index.html` |
| `WORKER_HEARTBEAT_FILE` | (unset) | File the worker refreshes while healthy; `python -m app.worker --check` exits 1 when it is stale |

Health checks: `GET /api/v1/health` (the process answers) and
`GET /api/v1/health/ready` (database reachable and schema at the newest
migration).

## Repository layout

```
backend/app/ai/           model clients (OpenAI, demo), prompts, pricing, budgets
backend/app/tailoring/    job analysis, questions, drafting, validation, review and approval
backend/app/render/       standard layout, in-place DOCX editing, one-page fitting
backend/app/ats/          ATS estimate: matching, passages, rubric scoring, improvement rules
backend/app/retrieval/    local embeddings, fact index, exact/full-text/semantic search, selection
backend/app/jobs/         durable job queue, runner, handlers
backend/app/api/          FastAPI routes (/api/v1), error envelope, middleware
backend/app/auth/         session cookies, JWT verification, CSRF, rate limits
backend/app/db/           SQLAlchemy models and per-user sessions
backend/app/schemas/      Pydantic request and response models
backend/app/documents/    uploads, extraction, DOCX editing, conversion, PDF inspection
backend/migrations/       Alembic migrations (schema, roles, RLS, storage policies)
backend/tests/            pytest suite
backend/scripts/          setup, proof, evaluation, and smoke-test scripts
backend/evaluation/       labeled fictional retrieval set and metrics
frontend/src/             React + TypeScript app (API client, auth, pages, components)
supabase/                 local Supabase configuration and email templates
docker/converter/         LibreOffice + fonts converter image
docs/decisions/           architecture decision records (ADR 0001-0012)
docs/learning-log.md      plain-language notes on the concepts used
```

## Updating dependencies

Python: edit `backend/requirements.in` or `requirements-dev.in`, then re-lock
(from `backend/`; slow, it hashes every platform's wheels):

```bash
.venv/Scripts/pip-compile --allow-unsafe --generate-hashes --strip-extras -o requirements.txt requirements.in
```

```bash
.venv/Scripts/pip-compile --generate-hashes --strip-extras -o requirements-dev.txt requirements-dev.in
```

`--allow-unsafe` pins `setuptools`, which torch requires on Python 3.12+.

JavaScript: install exact versions so `package.json` and `package-lock.json`
stay pinned, for example `npm install --save-exact <package>@<version>`.
