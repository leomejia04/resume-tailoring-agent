# ADR 0012: Hosted beta

Status: hosting cancelled (2026-10-12). The owner decided not to host the
app; it stays a local application in its GitHub repository. Kept: production
hardening (sections 1-5, same-origin serving, readiness and heartbeat
checks), the worker restart behavior (section 7), account deletion
(section 8), and `scripts/smoke_test.py` as a local end-to-end check.
Removed from the repository: the container images and Compose files, the
Linux lockfile, the production Caddy/Compose setup, the CI workflow, the
deployment security script, and the deployment guide (sections 6, 9, 10).
The text below records what was decided and verified at the time.

## Context

Milestones 1-7 run locally. Milestone 8 makes the app reachable over HTTPS
by a small number of real users with the same guarantees as local: private
data, bounded spend, jobs that survive restarts, documented rollback. Paid
provisioning and publication need the owner's explicit authorization; all
preparation is verified locally first.

## Decisions

### 1. One origin: the API serves the built frontend

`FRONTEND_DIST` points the API at the `npm run build` output, and
`app/web.py` serves it. Pages and `/api` share one origin, so the session
cookies (`__Host-`, SameSite=Strict) and the CSRF Origin check behave exactly
as with the Vite proxy in development, and CORS stays off. A separate static
host would need an `/api` rewrite or cross-origin credentials, which is more
configuration and more ways to weaken cookie or CSRF protection. CLAUDE.md
asks for inexpensive static frontend hosting; serving a few hundred KB of
static files from the API container costs nothing extra, and a CDN can still
be put in front later.

Details:

- Everything under `/api` goes to the API untouched (its JSON 404/405 stay).
  Only GET/HEAD of other paths are served by the frontend layer.
- The build's file list is read once at startup; a request can only name a
  file that was in the build (no path joining with client input, so no
  traversal). Unknown `/assets/...` paths return 404, never the app page
  with a 200. Every other path returns `index.html` (client-side routes).
- Hashed assets: `Cache-Control: public, max-age=31536000, immutable`;
  `index.html`: `no-cache` so a new deploy's asset names are picked up.
- Pages get their own CSP: `default-src 'self'; script-src 'self';
  style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src
  'self'; object-src 'none'; base-uri 'none'; form-action 'self';
  frame-ancestors 'none'`. No `unsafe-inline`/`unsafe-eval`: the Vite build
  has no inline scripts, and React's `style={{...}}` goes through the DOM,
  which `style-src` does not block. API responses keep
  `default-src 'none'`; the PDF preview route keeps its same-origin framing
  exception. All other security headers (nosniff, `X-Frame-Options: DENY`,
  Referrer-Policy, COOP/CORP, Permissions-Policy, HSTS in production) apply
  to pages too.
- A `FRONTEND_DIST` without `index.html` fails at settings load, not on the
  first request.

### 2. Readiness separate from liveness

`GET /api/v1/health` stays a liveness check. `GET /api/v1/health/ready`
returns 200 only when the database answers and `app.alembic_version` is at
the newest migration this build ships; otherwise 503 with which check failed
(`database`, `migrations`: `ok`/`failed`/`behind`), never why (details go to
the log). Migration `0010` grants `app_backend` SELECT on that one table.
A revision the build does not know counts as ready: it means a newer
release's migrations ran first (deploy order: migrate, then roll out), and
migrations must stay compatible with the previous release. Results are
cached for 5 seconds so probes cost at most one query per interval.

### 3. Worker liveness through a heartbeat file

With `WORKER_HEARTBEAT_FILE` set, the worker's main loop rewrites that file
(atomically) after each maintenance pass, only while every job thread is
alive. `python -m app.worker --check` exits 1 when the file is missing,
unreadable, or older than 90 seconds: the container health check. A file
rather than an HTTP port keeps the worker free of any listening socket.
Shutdown already finishes running jobs on SIGTERM; anything cut off is
recovered after its lease expires (ADR 0007).

### 4. Stricter production settings

In addition to the existing rules (HTTPS public origins, `TRUSTED_ORIGINS`,
Secure cookies, HTTPS Supabase URL, no demo AI, worker job runner),
production now refuses a missing `DATABASE_URL` or `SUPABASE_ANON_KEY` and
any secret still holding a `.env.example` placeholder (`replace-...`).

### 5. Client addresses behind the host's proxy

IP-based rate limits key on `request.client.host`. Uvicorn rewrites it from
`X-Forwarded-For` only for proxies listed in `FORWARDED_ALLOW_IPS` (its
`--proxy-headers` default is on). The deployment sets this to the host's
proxy addresses; `*` is acceptable only when the container is reachable
solely through that proxy, which the deployment guide must confirm per host.

### 6. Two images from one recipe, installed from a Linux lock

`docker/backend/Dockerfile` builds `web` and `worker` from a shared runtime
stage (Python 3.13 slim on Debian trixie and Node 24 for the frontend build,
both pinned by digest; non-root uid 10001; no compilers in the final
images). `.dockerignore` is an allow-list, so `.env`, caches, models, and
generated resume files can never enter a layer.

The Windows lock cannot be reused: on Linux, PyPI's torch wheel depends on
the CUDA stack (gigabytes of `nvidia-*` packages a CPU host never uses).
`backend/requirements.linux.in` takes every pin from `requirements.txt`
(`-c`) and only swaps in `torch==2.14.0+cpu`, which only PyTorch's CPU index
serves. The lock is compiled inside the same Linux base image with
`--generate-hashes`; the result matches the Windows lock exactly except
`torch==2.14.0+cpu` and the Windows-only `colorama`/`tzdata` (no CUDA
packages, no new licenses). Images install it with `--require-hashes
--no-deps`. Both images carry torch (one lock, simpler upkeep); the web
process only imports it if a request runs retrieval inline, which production
forbids.

The worker installs the same Debian font packages as `docker/converter` so
page counts match local rendering, runs LibreOffice in-process
(`CONVERTER=local`, since hosts offer no Docker socket), and bakes in the
embedding snapshot through `scripts.fetch_embedding_model` (every file
SHA-256 checked; runtime offline). `docker-compose.yml` runs both images
against the local Supabase stack with a read-only root filesystem, a private
`/tmp`, no capabilities, and memory caps.

### 7. A worker that dies is restarted by the host

Most hosts (Fly.io, Render) run no command health checks for a process
without an HTTP port. If a job thread dies, the worker now stops and exits
with status 1 so the host's restart policy replaces it; claimed jobs are
recovered when their leases expire. The heartbeat file stays for hosts that
do run `HEALTHCHECK` commands.

### 8. User-owned account deletion before a second user

CLAUDE.md requires deletion with confirmation, a retention explanation, and
cleanup of derived data; it was deferred until now and is built before
anyone else is invited. Settings → "Delete your account" explains what is
removed and what is retained (7-day database backups, OpenAI's API policy,
PDFs already downloaded), then needs the current password and the typed word
DELETE. `POST /api/v1/account/deletion`:

1. Re-checks the password with Supabase (rate-limited per user and IP; the
   check's own session is signed out). Wrong password: 403
   `password_incorrect`.
2. Under the owner's job lock, refuses while any job is queued or running
   (409 `jobs_active`), then inserts `app.account_deletions`. From then on
   `enqueue` refuses new jobs (409 `account_deleting`).
3. Deletes every object under the owner's folder in both buckets with the
   user's own session, then lists again to confirm nothing is left (503 and
   retry otherwise). Files stay immutable in normal use: the new Storage
   DELETE policy applies only while the marker exists, checked through
   `app_storage.account_deletion_requested()` (SECURITY DEFINER in its own
   schema, because `authenticated` has no access to `app`).
4. Calls `app.delete_current_account()`, which deletes the auth user named by
   the session's `app.current_user_id()`; every app row cascades (all app
   tables reference `auth.users` with `ON DELETE CASCADE`; the owner-scoped
   foreign keys between them are `NO ACTION`, checked at statement end, so
   one cascade removes them together). Open budget reservations are first
   subtracted from the app-wide daily total.

The backend still never holds the service-role key. Supabase lets the
migration role delete from `auth.users` but not grant that right, so the
function is owned by the migration role: a fixed body that can only delete
the current session's own user, only after step 2, executable only by
`app_backend`. Each step can be repeated if a later one fails.

### 9. CI and a deployment security check

`.github/workflows/ci.yml` (actions pinned to commit SHAs, read-only token):
ruff, the full pytest suite against a throwaway local Supabase stack (demo
AI, no secrets), the deployment security check against it, frontend
typecheck/lint/tests/build, and both image builds with smoke tests. The repo
has no GitHub remote yet, so it has not run.

`scripts/verify_deployment_security.py` checks a deployment read-only:
runtime role unprivileged and owning nothing, RLS enabled and forced on
every app table, Supabase API roles without access to `app`, rows hidden
without an identity, migrations current, buckets private (with an optional
admin connection), HTTP→HTTPS redirect, page CSP and security headers, HSTS,
no server banner, health and readiness, docs hidden, protected routes 401,
foreign-origin POST 403, no CORS for foreign origins, PostgREST not exposing
app tables, anonymous bucket listing empty, no extra sign-in providers, email
confirmation on. Exit code 1 on any failure; it never prints secrets.

### 10. Hosting choice: free tiers (owner's decision, 2026-10-12)

The owner chose the cheapest option and preferred free tiers. Checked on the
vendors' pages that day:

- No container host offers a free, always-on background worker with enough
  memory: Render's free tier has no background workers and sleeps web
  services after 15 minutes; Fly.io has no free tier for new organizations;
  Railway gives a one-time credit; Northflank's free sandbox is documented as
  not for production.
- **Oracle Cloud Always Free** gives an Arm VM (`VM.Standard.A1.Flex`, now
  2 OCPU and 12 GB, cut from 4/24 in June 2026) with 200 GB of disk and
  10 TB/month outbound, at $0. One VM runs Caddy (automatic Let's Encrypt
  certificates, HTTP to HTTPS), web, and worker with
  `deploy/docker-compose.prod.yml`; only Caddy publishes ports. The images
  build natively there: base images are multi-platform and the Linux lock
  hashes PyTorch's `2.14.0+cpu` aarch64 wheel (confirmed on the CPU index).
- **Supabase Free** for Auth, Postgres, and Storage at $0.
- Email through the owner's Gmail as custom SMTP (an app password the owner
  enters in Supabase); Supabase's built-in sender reaches only team members.

Accepted risks, stated in the guide: this is a beta, not reliable production.
Oracle may reclaim an Always Free instance that looks idle for 7 days (CPU,
network, and memory all under 20%), which a quiet beta can; upgrading the
account to Pay As You Go keeps the same resources at $0 and is recommended.
Supabase Free pauses after a week without activity (the owner restores it)
and keeps no backups, so the owner takes weekly `pg_dump` copies until the
project moves to Pro ($25/month). Cheapest paid fallback for the containers:
Fly.io (about $17/month for both machines). Nothing is created without the
owner: they sign up for Oracle and Supabase themselves (card details,
passwords, and the Gmail app password never pass through Claude).

## Verification (2026-10-12)

- `tests/test_hosting.py`: page routes, asset caching, missing assets,
  traversal attempts, API 404/405 shape preserved, non-GET never served the
  page, CSP without unsafe sources, settings refusals, readiness (503
  without a database, caching, migration states, 200 against the local
  stack as `app_backend`), heartbeat round trip, dead thread, garbage file,
  `--check` exit codes. The local-stack readiness test fails (503,
  `ProgrammingError`) with migration 0010 downgraded and passes after
  upgrading again.
- `tests/test_isolation.py` used to assert that `app_backend` cannot read
  `app.alembic_version`. That case now asserts it cannot UPDATE, DELETE or
  INSERT there (reading the version number exposes no user data; writing it
  could mislead migrations). Full suite: 351 passed with only that case
  failing before the change; the replaced cases pass (354 tests).
- Browser check of the real build served by the API on port 8002
  (`site-same-origin` in `.claude/launch.json`): dashboard and run pages
  render with no CSP violations, the PDF preview image loads, a
  CSRF-protected POST succeeds with the token and gets 403 without it, page
  headers carry the page CSP and `no-cache`.

## Verification (2026-10-12, session 9: containers, deletion, CI, docs)

- Linux lock compiled in `python:3.13-slim-trixie`: same pins as the
  Windows lock except `torch==2.14.0+cpu` and Windows-only packages; no
  `nvidia-*`/`triton`.
- `resume-web:dev` and `resume-worker:dev` built. Web image: uid 10001,
  `torch 2.14.0+cpu`, built frontend present, no model, no CUDA packages.
  Worker image on a read-only root filesystem, no capabilities,
  `no-new-privileges`: pinned model verified, `CONVERTER=local`, fixture DOCX
  converted in 1.7 s to a one-page PDF in Liberation Sans, font package
  versions recorded in `/etc/converter-versions.txt`.
- `docker compose up`: both containers started, `/api/v1/health/ready` 200
  (database ok, migrations ok), worker started. Idle memory: worker about
  86 MB, web about 101 MB (`docker stats`). The first smoke-test run found a
  real problem: inside containers Supabase is reached as
  `host.docker.internal`, but its tokens name `127.0.0.1`, so every session
  was rejected. Fixed with `SUPABASE_JWT_ISSUER` (expected issuer; JWKS still
  fetched from `SUPABASE_URL`), refused in production where both names
  match. The rerun was cut off: Docker Desktop stopped responding when drive
  C: ran out of space. **Still owed:** the compose smoke test
  (`scripts.smoke_test --delete-account` against port 8003) and the worker's
  peak memory during a draft, render, and check, with and without
  `RETRIEVAL_MODE=hybrid` (model loaded).
- Account deletion: `tests/test_account_deletion.py` (4 tests: rows, files
  two folders deep, and sign-in removed while a second account is untouched;
  wrong password 403 and bad confirmation 422 change nothing; active jobs 409
  before anything is removed; the function refuses without identity or
  marker; a marked account cannot queue jobs; owners cannot delete files
  before requesting deletion). `DeleteAccount.test.tsx` (2). Migration
  0011 → 0010 → 0011 round trip verified by catalog queries, `alembic check`
  clean. Browser check on the API-served build: wrong password message,
  deletion, sign-in page confirmation, content-free server log.
- `tests/test_verify_deployment.py` (3) and the script against the local
  stack: 24 checks, 0 failed, 4 skipped (HTTPS-only); pointed at the schema
  owner as runtime role it fails 3 checks as it should.
- Worker exit on a dead thread: 2 tests in `tests/test_hosting.py`.
- Full backend suite 363 passed (before the issuer setting); afterwards the
  279 non-integration tests and the new issuer test pass; frontend 85 passed,
  typecheck, lint, build.

## Not yet decided

Nothing open on design. Waiting on the owner: Oracle Cloud and Supabase
sign-ups, the domain, the Gmail app password in Supabase, and pushing to
https://github.com/leomejia04/resume-tailoring-agent so CI runs.
