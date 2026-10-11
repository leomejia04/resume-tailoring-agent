# ADR 0007: Durable jobs and the worker

Status: accepted (milestone 6c).

## Context

Job analysis, drafting, section rewrites, and chat revisions call a paid
model (seconds to a minute); rendering runs LibreOffice in a container (10-60
seconds); ATS re-checks read stored files. Doing this inside web requests
ties up the request, can exceed hosting request limits, and loses work (and
held budget) when a process restarts. The specification requires a durable
worker with legal run states, cancellation, bounded retries, restart
recovery, duplicate protection, and atomic limits, and asks to avoid extra
queue infrastructure when a simpler durable solution works.

## Decisions

1. **PostgreSQL is the queue.** `app.jobs` holds kind, lane, status
   (`queued`, `running`, `succeeded`, `failed`, `cancelled`), small validated
   params (IDs and versions, never file contents or secrets), attempts,
   `run_after`, a lease (`locked_by`, `lease_expires_at`, `heartbeat_at`),
   `cancel_requested`, an error code, and a small result. No broker, no
   extra service; jobs survive restarts because they are rows.
2. **Claiming without double work.** Workers call `app.claim_job`, which
   picks the oldest due job with `FOR UPDATE SKIP LOCKED` (concurrent workers
   skip rows another worker is claiming) and sets a lease. While running, a
   heartbeat thread renews the lease every third of its length and reads the
   cancel flag. Finishing is fenced on `locked_by`: a worker whose lease
   expired and was taken over cannot overwrite the new attempt's outcome.
3. **Least privilege for the queue.** Cross-owner queue work needs to see
   every owner's queued rows, so it happens only in three fixed SQL functions
   (`claim_job`, `recover_expired_jobs`, `release_expired_reservations`)
   owned by `app_queue`: a NOLOGIN role with no RLS bypass, privileges only on
   queue columns and spending counters, and explicit RLS policies. Function
   privileges are set before ownership moves (afterwards the migration role's
   REVOKEs would be silently ignored); only `app_backend` may execute them.
   Everything a job does after claiming runs as its owner
   (`app.user_id` = owner, forced RLS), exactly like a request. Never a
   service role.
4. **Files through signed per-object URLs** (owner's decision). A worker has
   no user session. When a request queues work that needs files, it signs
   URLs with the user's own session for exactly those objects (read this
   master, read this PDF, create these PDF/DOCX paths), so Storage policies
   decide what may be signed. They are stored in `app.job_grants` (RLS,
   never returned by the API, never logged: `httpx` request logging stays
   off in the worker too) and deleted when the job finishes. The worker
   accepts only relative `/object/...` paths and prefixes its own
   `SUPABASE_URL` (no SSRF through stored URLs). Signed uploads can create
   but never overwrite an object, so every render attempt has its own
   artifact ID and paths. A job that starts after its grants expired (about
   an hour for reads, two hours for uploads) fails with `grant_expired`; the
   user retries.
5. **Retries are bounded and selective.** Up to `JOB_MAX_ATTEMPTS` (3)
   attempts with exponential backoff (`JOB_RETRY_BACKOFF_SECONDS` * 2^n) for
   transient failures only: model timeouts, unavailability, and rate limits;
   storage, converter, and database hiccups. Refusals, invalid output,
   budget limits, conflicts, invalid states, and expired grants fail at once.
   Every paid attempt is still reserved and accounted.
6. **Run states.** New `queued` state (run created, analysis waiting).
   Analysis moves `queued -> analyzing -> awaiting_confirmation | ready`;
   drafting moves through `drafting -> validating -> awaiting_review`. A
   retried job resumes from `analyzing` or `drafting`/`validating` left by a
   crashed attempt. When a job finally fails, the handler leaves an honest
   state: the run becomes `failed` with `analysis_<code>` or `draft_<code>`;
   a failed chat adds an assistant message saying nothing changed; failed
   rewrites, renders, and checks leave the run as it was and show the failed
   job. Rendering and scoring are job kinds rather than run states, so a
   draft under review can be rendered without leaving review.
7. **Duplicate protection and limits.** A partial unique index allows one
   active job per run and lane (`workflow` for analysis, drafting, rewrites,
   and chat; `render`; `ats`): a second click gets 409 `job_in_progress`.
   Enqueueing takes a per-owner advisory lock, then refuses more than
   `MAX_ACTIVE_JOBS_PER_USER` (4) active jobs with 429. Claiming prefers
   owners with fewer than `MAX_RUNNING_JOBS_PER_USER` (1) running jobs (a
   soft fairness rule; the active limit is the hard one). Existing rate
   limits and spending caps still apply.
8. **Cancellation.** Cancelling a run cancels its queued jobs immediately
   (and deletes their grants) and flags running ones; the worker stops at
   its next checkpoint, and any save into a cancelled run is refused by the
   run's state machine, so nothing is written afterwards.
9. **Restart recovery.** Every 15 seconds the worker runs
   `recover_expired_jobs` (running jobs whose lease expired go back to
   `queued` with backoff, or to `failed` with `lease_expired` once out of
   attempts) and `release_expired_reservations`. Budget reservations are now
   rows with an expiry (`RESERVATION_TTL_SECONDS`, 15 minutes); settling or
   releasing deletes the row and subtracts only what the delete found, so a
   reservation is returned exactly once even when a crashed call settles
   late. The migration marks runs left in `analyzing`/`drafting`/`validating`
   by the old synchronous code as `failed` (`interrupted`).
10. **API and UI.** Action routes (create run, draft, rewrite, chat, render,
    ATS check) validate synchronously (404/409/429 at once) and answer
    `202 Accepted` with the run, which now lists the latest job of each kind.
    The run page polls `GET /runs/{id}` every 1.5 seconds while any job is
    queued or running; panels show waiting, working, and retrying states and
    a short failure sentence from the job's error code. Direct edits pause
    while the AI revises the draft, so they cannot conflict with its result.
11. **Two runners, one code path.** `JOB_RUNNER=worker` (default; the only
    value allowed in production) leaves jobs to `python -m app.worker`.
    `JOB_RUNNER=inline` makes the request claim and run its own job through
    the same runner before answering, retrying immediately; integration tests
    use it, and `tests/test_jobs.py` drives the real worker functions.

## Verified

`tests/test_jobs.py`: end to end through the worker (analysis with the
master read through a signed URL, duplicate draft refused, render uploaded
through signed URLs, grants deleted, approval), crash recovery with fencing,
a job that keeps crashing fails the run with `analysis_lease_expired`,
cancellation of queued and running jobs, the per-user limit with rollback,
expired reservations returned exactly once, expired grants failing without
retries, and privileges (no cross-owner reads, no `SET ROLE app_queue`, no
updates to job owner/params or grant URLs, Supabase API roles cannot execute
the queue functions); units for URL validation, production refusing inline
jobs, and retry classification. Existing integration tests run through the
inline runner; the model-timeout test now shows three accounted attempts.

## Limitations

- Fencing protects the job row, not every side effect: if a lease expires
  while a slow attempt is still running, two attempts can overlap. Draft
  saves are protected by expected versions and renders use distinct
  artifact paths, so the worst case is an extra, unused artifact or a
  refused save.
- Objects uploaded by an attempt that then failed are orphans until the
  deferred orphan cleanup exists.
- The per-owner running limit is checked at claim time without a lock, so
  two workers can briefly run two jobs for one owner.
- Polling is simple and cheap at this scale; server-sent events can replace
  it later without changing the job model.
- Hosting the worker (a second process or container with the converter) is
  part of milestone 8.
