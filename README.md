# Resume tailoring app

A hosted, multi-user application for creating truthful, tailored, one-page PDF
resumes. The full specification and build progress live in [CLAUDE.md](CLAUDE.md).

**Current state:** milestones 1-5 are complete (milestone 4's live `gpt-5.6-luna` smoke test passed on fictional data on 2026-10-11). Milestone 6 is in progress: ATS match estimates are scored, saved, and required for approval (6a-6b), and slow or paid steps run in a durable background worker with bounded retries and crash recovery (6c); automatic improvements and the application tracker are next.

- Document proof: a fictional master DOCX is edited in a copy with formatting
  preserved, rendered to PDF in a sandboxed LibreOffice container, and inspected.
- Web app shell: React frontend and FastAPI backend connected through the Vite
  dev proxy.
- Accounts and private data: sign-up/sign-in/password reset (Supabase Auth,
  HttpOnly cookie sessions, CSRF protection, rate limits), profile, immutable
  versioned master uploads in private storage, and an experience bank of
  extracted facts to confirm, edit, or reject. Two-user isolation is proven at
  the API, database (row-level security), and storage layers.
- AI tailoring baseline: saved job postings, cached job analysis, precise
  questions about missing skills, and grounded drafts in which every bullet
  cites confirmed facts. Deterministic checks remove unsupported numbers,
  tools, dates, and titles; costs are recorded and capped per user and per day.
- Review and export: edit, reorder, add or remove facts, rewrite a section,
  ask for changes in chat (all re-checked against confirmed facts), compare
  and restore versions, set formatting preferences, render the real one-page
  PDF in your own DOCX design (or a labeled rebuilt layout), approve the exact
  PDF, and download it as `Prefix_Position_Company_CV.pdf`.
- ATS match estimate: a 0-100 estimate from our documented rubric (ADR 0006)
  for the master resume (baseline), every draft version, and the exact final
  PDF, with requirement-by-requirement evidence, must-have gaps, and the
  master-vs-tailored comparison. Approval requires a complete check of the
  exact PDF. It is not an employer's score.

## Repository layout

```
backend/app/ai/           model clients (OpenAI, demo), prompts, pricing, budgets
backend/app/tailoring/    job analysis, gaps/questions, drafting, validation, review/approval
backend/app/render/       template-aware and reconstructed DOCX rendering, one-page fitting
backend/app/ats/          ATS match estimate: term matching, passages, rubric scoring (ADR 0006)
backend/app/api/          FastAPI routes (/api/v1), error envelope, middleware
backend/app/auth/         session cookies, JWT verification, CSRF, rate limits
backend/app/db/           SQLAlchemy models and per-user sessions
backend/app/schemas/      Pydantic request/response models
backend/app/services/     deterministic job-text processing
backend/app/documents/    DOCX editing, verification, conversion, PDF inspection, uploads, extraction
backend/migrations/       Alembic migrations (schema, roles, RLS, storage policies)
backend/tests/            pytest suite (test_isolation.py needs the local Supabase stack)
backend/scripts/          document_proof.py, local_env.py
frontend/src/             React + TypeScript app (api client, auth, pages, components)
supabase/                 local Supabase configuration and email templates
docker/converter/         LibreOffice + fonts converter image
docs/decisions/           architecture decision records
docs/learning-log.md      plain-language notes on concepts used
```

## Prerequisites (Windows)

- Python 3.12+ (developed on 3.13)
- Node.js 24 LTS (`frontend/package.json` requires >= 24.15; several
  dependencies require Node 22.22+ or 24.15+)
- Docker Desktop with the WSL 2 engine (runs LibreOffice; LibreOffice does not
  need to be installed on Windows)
- Git

## Setup

Commands were run from Git Bash on Windows. In PowerShell, use backslashes
(`.venv\Scripts\python`).

Build the converter image (repository root):

```bash
docker build -t resume-converter:dev docker/converter
```

Backend environment (from `backend/`):

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install --require-hashes -r requirements.txt -r requirements-dev.txt
```

Frontend dependencies (from `frontend/`), installed exactly from the lockfile:

```bash
npm ci
```

### Local Supabase (accounts, database, storage)

Install the pinned Supabase CLI (repository root):

```bash
npm ci
```

Create your own local JWT signing key (repository root; the file is
git-ignored and contains a private key):

```bash
echo "[]" > supabase/signing_keys.json
```

```bash
npx supabase gen signing-key --algorithm ES256 --append
```

Start the stack (first run downloads several container images):

```bash
npx supabase start
```

Write `.env` in the repository root from the running stack (from `backend/`;
generates a random password for the `app_backend` database role and refuses to
overwrite an existing `.env`):

```bash
npx --prefix .. supabase status -o env | .venv/Scripts/python -m scripts.local_env
```

Apply database migrations (from `backend/`):

```bash
.venv/Scripts/alembic upgrade head
```

Stop the stack when finished (data is kept and restored on the next start):

```bash
npx supabase stop
```

Local tools while the stack runs: Studio (database browser) at
http://127.0.0.1:54323 and Mailpit (captured emails, e.g. password resets) at
http://127.0.0.1:54324.

## Run the app locally

Start the API (from `backend/`):

```bash
.venv/Scripts/python -m uvicorn app.main:create_app --factory --reload --port 8000 --no-access-log --no-server-header
```

Start the background worker in a second terminal (from `backend/`). It runs
job analysis, drafting, rewrites, chat revisions, PDF rendering, and ATS
checks; without it those actions stay "Waiting to start":

```bash
.venv/Scripts/python -m app.worker
```

Start the frontend in a third terminal (from `frontend/`):

```bash
npm run dev
```

Open http://127.0.0.1:5173, create an account, upload a master resume on the
Profile page, and review its facts on the Experience page. To try tailoring
without an OpenAI key, add `LLM_PROVIDER=demo` to `.env` (deterministic rules,
clearly labeled as not AI); with `OPENAI_API_KEY` set, the configured OpenAI
model is used instead. The page header
shows "Server connected" when the frontend can reach the API. Vite forwards `/api` requests to
`http://127.0.0.1:8000`, so the browser uses a single origin and no CORS
configuration is needed. API docs (development only): http://127.0.0.1:8000/api/docs

Windows tips:

- Stop servers with Ctrl+C. With `--reload`, uvicorn runs a watcher plus a
  worker process; force-killing only the watcher leaves the worker holding
  port 8000.
- The background worker stops with Ctrl+C after finishing its current jobs.
  If it is killed mid-job, the next worker retries that job once its lease
  (60 seconds) expires.
- If HTTPS calls to OpenAI fail with a certificate error, antivirus HTTPS
  scanning (e.g. Norton) is re-signing traffic: set `USE_OS_TRUST_STORE=true`.
- Stop `npm run dev` before running `npm ci`: Windows locks native modules
  that a running process has loaded, and `npm ci` fails with `EPERM`.

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

`tests/test_isolation.py` runs against the local Supabase stack and `.env`;
it is skipped automatically when they are unavailable. The PDF render tests
need Docker and the converter image.

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

Document proof (from `backend/`; writes files to git-ignored `backend/out/proof/`):

```bash
.venv/Scripts/python -m scripts.document_proof
```

## Configuration

Environment variables (see [.env.example](.env.example)):

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_ENV` | `development` | `production` disables API docs and enables HSTS |
| `CORS_ALLOWED_ORIGINS` | (empty) | Exact origins allowed cross-origin; empty means same-origin only |
| `MAX_REQUEST_BYTES` | `262144` | Maximum request body size |
| `CONVERTER` | `auto` | `auto` uses a local `soffice` if found, else Docker; or `local` / `docker` |
| `CONVERTER_IMAGE` | `resume-converter:dev` | Docker image used by the `docker` backend |
| `SOFFICE_PATH` | (unset) | Path to a local `soffice` executable |
| `CONVERSION_TIMEOUT_SECONDS` | `60` | Hard limit per conversion |
| `SUPABASE_URL` | `http://127.0.0.1:54321` | Supabase project URL |
| `SUPABASE_ANON_KEY` | (unset) | Publishable key; auth and storage are unavailable without it |
| `DATABASE_URL` | (unset) | Runtime connection as the least-privilege `app_backend` role |
| `MIGRATION_DATABASE_URL` | (unset) | Schema-owner connection, used only by Alembic |
| `APP_DB_PASSWORD` | (unset) | Password the migration sets for `app_backend` |
| `TRUSTED_ORIGINS` | Vite dev origins | Origins allowed to send state-changing requests; required in production |
| `COOKIE_SECURE` | auto | Secure cookies in production, plain on localhost |
| `MAX_UPLOAD_BYTES` | `5242880` | Maximum master upload size |
| `LLM_PROVIDER` | auto | Empty: OpenAI if a key is set, else AI off; `demo` for local testing (not allowed in production) |
| `OPENAI_API_KEY` | (unset) | Server-side OpenAI key |
| `OPENAI_MODEL` | `gpt-5.6-luna` | Verified on OpenAI's model page (2026-10-11) |
| `OPENAI_REASONING_EFFORT` | `low` | Benchmark starting point |
| `USE_OS_TRUST_STORE` | `false` | `true` verifies HTTPS with the operating system's certificates (needed when antivirus such as Norton scans HTTPS); verification stays on |
| `USER_DAILY_BUDGET_USD` / `APP_DAILY_BUDGET_USD` | `0.50` / `5.00` | Atomic spending limits checked before every model call |
| `RESERVATION_TTL_SECONDS` | `900` | An unsettled reservation (crash mid-call) is returned after this |
| `JOB_RUNNER` | `worker` | `worker`: `python -m app.worker` runs jobs; `inline`: the request runs its own job (tests only; refused in production) |
| `JOB_MAX_ATTEMPTS` / `JOB_RETRY_BACKOFF_SECONDS` | `3` / `5` | Bounded retries for transient failures (backoff doubles) |
| `JOB_LEASE_SECONDS` | `60` | How long a silent worker keeps a job before another may retry it |
| `WORKER_CONCURRENCY` / `WORKER_POLL_SECONDS` | `2` / `1.0` | Worker threads and idle polling interval |
| `MAX_ACTIVE_JOBS_PER_USER` / `MAX_RUNNING_JOBS_PER_USER` | `4` / `1` | Per-user queue limit (429) and fair-share running limit |
| `GRANT_TTL_SECONDS` | `3600` | Lifetime of signed download URLs handed to a job |
| `PRICE_*_PER_MTOK` | official rates | Used for cost accounting |

## Updating dependencies

Python: edit `backend/requirements.in` or `requirements-dev.in`, then re-lock
(slow: hashes every platform's wheels):

```bash
.venv/Scripts/pip-compile --generate-hashes --strip-extras -o requirements.txt requirements.in
```

```bash
.venv/Scripts/pip-compile --generate-hashes --strip-extras -o requirements-dev.txt requirements-dev.in
```

JavaScript: install with exact versions so `package.json` and
`package-lock.json` stay pinned, e.g. `npm install --save-exact <package>@<version>`.
