# ADR 0009: Manual application tracker

Status: accepted (milestone 6e).

## Context

Version one needs a manual tracker: company, role, posting URL, the approved
resume version that was sent, application date, status, and notes. It must
not send anything: no Google Drive, auto-apply, recruiter messages, or
reminders. Every user's tracker is private. An approval already binds one
draft version to the SHA-256 of its exact PDF (ADR 0005), so "which resume
did I send?" can point at that record and need not copy it.

## Decisions

1. **One table, owner-scoped like everything else.** `app.applications` has
   company and role (required, 200 characters), `job_url` (2048), an optional
   `approval_id`, `applied_on` (a calendar date, no time zone), `status`,
   `notes` (4000), and `lock_version`. Forced row-level security, plus an
   explicit owner filter in every query.
2. **The resume link is the approval, made safe by the schema.** A composite
   foreign key `(approval_id, owner_id) -> approvals(id, owner_id)` (new
   unique constraint `uq_approvals_id_owner`) makes a link to another user's
   approval impossible, even for code that bypasses the API. The API checks
   first, so an unknown or foreign ID gets a field error on `approval_id`
   (422), not a database error. Approvals are insert-only, so a link never
   dangles. Removing a link or deleting an entry never touches the approval
   or its PDF.
3. **Statuses:** `planned`, `applied`, `interviewing`, `offer`, `accepted`,
   `rejected`, `withdrawn`. The date is optional (planned entries have none).
   It must fall between 2000-01-01 and tomorrow (UTC), which leaves one day of
   slack for time zones ahead of the server.
4. **Optimistic concurrency for every change.** `PUT` replaces all fields and
   `DELETE` takes `?expected_version=`. Each runs one conditional statement
   (`WHERE lock_version = expected`). Zero rows means 409 `conflict` if the
   entry still exists and 404 if not, so a stale tab can neither overwrite
   nor delete a newer version. Quick status changes from the list send the
   whole entry with its version, through the same route.
5. **Hard delete, confirmed in the UI.** Entries are the user's own notes,
   not evidence or history, so deleting one removes the row. The page asks
   first ("Delete permanently" or "Keep", with focus on Keep).
6. **Column-limited privileges.** `app_backend` may select, insert, delete,
   and update only the editable columns. `id`, `owner_id`, and `created_at`
   cannot change. A check constraint keeps non-http(s) links out of the
   table even if the API were bypassed.
7. **An entry cap per user, enforced atomically.** `MAX_APPLICATIONS_PER_USER`
   (default 1000) bounds free-text storage. Inserts take a per-owner
   transaction advisory lock before counting, as the job queue does, so
   concurrent requests cannot overshoot. Beyond the cap: 409 `tracker_full`.
8. **Links are labels.** Posting URLs are validated (http/https only, no
   embedded credentials, length cap) and stored, never fetched. The UI makes
   them clickable only if they parse as http(s). It opens them in a new tab
   with `rel="noopener noreferrer"`. Notes render as escaped text with line
   breaks kept.
9. **Not evidence, not AI input.** Tracker entries never feed tailoring,
   retrieval, or any model call. The page says so. Request logs hold only
   method, path, status, timing, and request ID.
10. **Entry points.** "Track application" on each approved resume (dashboard)
    and "Track this application" after approval (run page) open the form
    prefilled from that job: company, role, posting link, and resume. The
    `?approval=` parameter stays until the form closes, so a reload reopens
    the same form.

## Verified

- Units (`tests/test_tracker.py`, 28): blank optional fields, trimming,
  control characters (line breaks allowed only in notes), link scheme,
  credentials, and length, the date range including tomorrow's slack, status
  values, forbidden extra fields (`owner_id`, `lock_version`), required
  `expected_version` on `PUT`/`DELETE`, no echo of rejected notes, 401
  without a session, 403 without a CSRF token, and a safe 503 without a
  database.
- Integration (`tests/test_tracker_flow.py`, 5, local Supabase as the runtime
  role):
  - create, list (most recent first, status filter), edit, delete;
  - stale `PUT` and `DELETE` get 409, and missing entries 404;
  - two users see and change only their own entries;
  - under RLS a user cannot read or change another user's rows, cannot insert
    rows claiming another owner, and cannot change `owner_id`, `id`,
    `created_at`, or store a `javascript:` link;
  - six concurrent inserts against a cap of 3 give exactly three 201s and
    three 409s. With the advisory lock temporarily removed, all six
    succeeded and the test failed, so the test detects the race;
  - linking a real rendered and approved resume works. Another user's
    approval and random IDs get 422. The composite foreign key refuses the
    cross-owner link in SQL. Unlinking keeps the approval downloadable.
- Frontend (`TrackerPage.test.tsx`, 8): add with today's date, client and
  server field errors with a focused summary, prefill from an approval link,
  safe links only, status filter and quick status change with the loaded
  version, a stale edit refused without losing the typed text, delete
  confirmation, and load errors with their reference. The export panel test
  covers "Track this application".
- Live in the browser (fictional local account, second local stack on ports
  8001/5174):
  - prefill from an approved resume, then save;
  - the linked PDF downloads under its approved filename;
  - HTML-like note text renders escaped;
  - an edit made through the API behind the page's back made the page's
    save fail with the conflict message, and the list reloaded;
  - quick status change and confirmed delete work, and the approvals remain;
  - focus returns to "Add application" after saving and deleting;
  - no horizontal scrolling at 375 px;
  - API logs held no entry content.

## Limitations

- No reminders, follow-up dates, status history, or export (out of scope
  for version one).
- One open form at a time; a stale edit cannot be merged and must be
  redone on the latest version.
- Account and data deletion (deferred) must also remove `applications`.
  Rows cascade from `auth.users`, but a partial "delete my resumes" feature
  would first need to unlink or delete entries that reference approvals.
