# ADR 0012: Hosted beta

Status: in progress (2026-10-12). Production hardening (step 2 of the
milestone 8 plan) is accepted; containers, hosting choice, CI and provisioning
are recorded here as they are done.

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

## Not yet decided

Container images and the Linux lockfile, worker memory measurement, hosting
provider (prices and terms to be verified on vendor pages), hosted Supabase
settings (redirect URLs, SMTP, JWT expiry), CI, backups/restore, deployment
guide, and whether minimal account deletion is required before inviting a
second user.
