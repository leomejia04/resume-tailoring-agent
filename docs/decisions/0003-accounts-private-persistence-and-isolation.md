# ADR 0003: Accounts, private persistence, and tenant isolation

Status: accepted (milestone 3)

## Context

Every user's profile, master versions, and experience facts must be private,
masters must stay immutable, and isolation must be proven before any public
exposure. CLAUDE.md forbids relying on a service role that bypasses
row-level security (RLS).

## Decisions

### Local Supabase first
The Supabase CLI (pinned `supabase@2.119.0` in the root `package.json`) runs the
real stack (PostgreSQL 17, Auth/GoTrue, Storage, Studio, Mailpit) in Docker.
Unused services (realtime, edge functions, analytics, S3 endpoint, vector
storage) are disabled in `supabase/config.toml`. JWTs are signed with a local
ES256 key (`supabase/signing_keys.json`, git-ignored, generated per developer)
so verification works exactly as on hosted Supabase (JWKS, asymmetric keys).
Auth settings: 12-character minimum password without composition rules,
recent sign-in required to change passwords, recovery emails link to
`/reset-password#token_hash=...` (fragment: never sent to servers or logged).

### Backend-for-frontend sessions
The browser never sees Supabase tokens. FastAPI calls Supabase Auth
server-to-server and stores the access and refresh tokens in cookies that are
HttpOnly, SameSite=Strict, Path=/, Secure in production, with the `__Host-`
prefix in production. Access tokens are verified locally against the
project's JWKS (`app/auth/tokens.py`): ES256/RS256 only (no HS256, no `none`),
issuer, audience `authenticated`, expiry, subject UUID, role `authenticated`,
and no anonymous users. Expired access tokens yield `401 session_expired`; the
frontend refreshes once (`POST /auth/refresh`, rotating refresh token) and
retries. Logout revokes the refresh token at Supabase and clears cookies.

### CSRF
`app/auth/csrf.py` guards every POST/PUT/PATCH/DELETE under `/api/`: the
`Origin` (or `Referer` when `Origin` is absent) must be a trusted origin and
the `X-CSRF-Token` header must equal the readable `rt_csrf` cookie (constant
time comparison). Login and signup are protected too (login CSRF).

### Public auth routes and abuse limits
Only `/health` and `/auth/*` work without a session. Signup, login, refresh,
and password-reset routes are rate-limited per IP and per email with shared
fixed-window counters in PostgreSQL (`app.rate_limit_counters`, atomic
`INSERT ... ON CONFLICT ... RETURNING`, SHA-256 keys, pruned after a day). If
the limiter cannot run, these routes fail closed with 503. Messages are
generic: wrong credentials, existing accounts, and unknown reset emails reveal
nothing.

### Database isolation (`app` schema)
- Tables live in a private `app` schema that Supabase's REST API does not
  expose; `anon`, `authenticated`, `service_role`, and `PUBLIC` get no grants.
- The runtime connects as `app_backend`: LOGIN, NOINHERIT, NOSUPERUSER,
  NOBYPASSRLS, not the table owner, with 15 s statement and 30 s
  idle-in-transaction timeouts. Migrations run as the schema owner through a
  separate `MIGRATION_DATABASE_URL`.
- Each request runs in one transaction that first calls
  `set_config('app.user_id', <verified user id>, true)`. RLS policies on every
  owned table compare the owner column to `app.current_user_id()`, and RLS is
  forced even for the table owner. The setting is transaction-local, so it
  cannot leak to the next request on a pooled connection. With no identity,
  no rows are visible.
- Application queries also filter by owner (defense in depth), and other
  users' resources return the same 404 as missing ones.
- Composite foreign keys `(child_id, owner_id) -> (id, owner_id)` make a row
  referencing another user's master or fact impossible.
- History is immutable by privilege: `app_backend` may only SELECT/INSERT
  `master_versions` and `evidence_versions`, may update only
  `evidence_records.current_version` and the editable profile columns, and has
  no DELETE on user data.
- Optimistic concurrency: profile updates and new fact versions succeed only
  for the version the client last saw (`409` otherwise), enforced by
  conditional `UPDATE ... WHERE version = expected` plus unique constraints.

### Private storage
A private `masters` bucket (5 MB, DOCX/PDF only) stores files at
`<user_id>/<master_version_id>.<ext>`. The backend calls Storage with the
user's own access token, so Storage RLS policies (insert/select only inside
the caller's own folder, no update/delete) enforce ownership independently of
our database. Upload order: database rows are flushed inside an open
transaction, the object is stored, then the transaction commits. A storage
failure rolls everything back; a commit failure after storage can leave a
private orphan object, logged by ID for cleanup.

### Uploads and extraction
`app/documents/uploads.py` decides the type from bytes (the client's
Content-Type is ignored and the extension must match), rejects legacy/
encrypted OLE files, macro-enabled or embedded-object DOCX, ZIP bombs (entry
count, total expansion, compression ratio), unsafe ZIP paths, encrypted PDFs,
and PDFs over 10 pages. Display names are sanitized labels; storage paths use
UUIDs. `app/documents/extract.py` deterministically proposes facts (bullets,
bullet-less entries such as degrees, skills lines) with section, entry, dates,
numbers found, and a suggested experience type. Every fact starts unverified;
confirming requires a non-"unclassified" type. Edited wording records
`change_source = user` while `source_text` keeps the original.

## Verified (2026-10-11, local stack)
`backend/tests/test_isolation.py` (27 integration tests) proves: API-level
isolation between two real accounts (lists, direct IDs, downloads, edits);
RLS hiding rows with no identity or the wrong identity; RLS rejecting rows that
claim another owner; impossible cross-owner references; the runtime role
cannot update/delete history, read migration state, assume Supabase roles,
create tables, or disable RLS; identity does not leak across pooled
connections; Storage blocks cross-user reads, uploads into another user's
folder, and public access; the REST API does not expose the `app` schema;
logout revokes refresh tokens; refresh rotates them; login is rate-limited;
and the full password-reset email flow works without account enumeration.
A manual browser run repeated the two-user check through the UI.

## Known limitations and follow-ups
- An access token stays valid until it expires (local `jwt_expiry` 3600 s)
  even after logout; consider 15 minutes on the hosted project.
- No account or data deletion yet (needs a carefully isolated admin path for
  auth users and storage objects; planned with Settings).
- No cleanup job for orphan storage objects yet (milestone 6 worker).
- Email confirmation is off locally; hosted production must enable it with a
  real SMTP provider. Consider CAPTCHA and leaked-password protection before
  public signup.
- Rate limits key on the client IP; production must run uvicorn with
  `--proxy-headers` and an explicit `--forwarded-allow-ips` list.
- DOCX/PDF parsing runs in the API process with XML entity resolution off;
  move parsing into the sandboxed worker in milestone 6.
- PDF extraction is heuristic and OCR is not available; the UI says so.
- Hosted Supabase is not provisioned: it needs the owner's authorization,
  project credentials, and the same migration run against it.
