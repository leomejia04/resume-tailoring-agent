# Resume tailoring app

A hosted, multi-user application for creating truthful, tailored, one-page PDF
resumes. The full specification and build progress live in [CLAUDE.md](CLAUDE.md).

**Current state:** milestones 1-3 are complete.

- Document proof: a fictional master DOCX is edited in a copy with formatting
  preserved, rendered to PDF in a sandboxed LibreOffice container, and inspected.
- Web app shell: React frontend and FastAPI backend connected through the Vite
  dev proxy.
- Accounts and private data: sign-up/sign-in/password reset (Supabase Auth,
  HttpOnly cookie sessions, CSRF protection, rate limits), profile, immutable
  versioned master uploads in private storage, and an experience bank of
  extracted facts to confirm, edit, or reject. Two-user isolation is proven at
  the API, database (row-level security), and storage layers.

## Repository layout

```
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

Start the frontend in a second terminal (from `frontend/`):

```bash
npm run dev
```

Open http://127.0.0.1:5173, create an account, upload a master resume on the
Profile page, and review its facts on the Experience page. The page header
shows "Server connected" when the frontend can reach the API. Vite forwards `/api` requests to
`http://127.0.0.1:8000`, so the browser uses a single origin and no CORS
configuration is needed. API docs (development only): http://127.0.0.1:8000/api/docs

Windows tips:

- Stop servers with Ctrl+C. With `--reload`, uvicorn runs a watcher plus a
  worker process; force-killing only the watcher leaves the worker holding
  port 8000.
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
