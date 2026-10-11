# ADR 0005: Review, rendering, approval, and downloads

Status: accepted (milestone 5)

## Decisions
1. **Immutable versions for every change.** Direct edits, restores, section
   regeneration, and chat revisions each append a draft version on top of the
   version the client last saw (`expected_version`; 409 otherwise, enforced by
   a conditional `UPDATE drafts ... WHERE current_version = expected`).
   Restoring an old version creates a new version with the old content.
2. **Same checks for people and models.** Edited bullets, skills, summaries,
   and headlines run through the validator used for generation (numbers,
   years, requirement terms, name-like tokens, headline words must be backed
   by the cited confirmed facts). Unsupported edits are rejected (422
   `unsupported_edit`) with the exact problem; nothing is saved. Facts can be
   added back only from the user's confirmed facts. Chat requests return a
   concise patch (changed entries, optional headline/summary) that is
   validated like a draft; unsupported parts are left out and the reply says
   so. The user's instruction is treated as untrusted data.
3. **Rendering.** DOCX masters use template-aware rendering: bullet
   paragraphs of the user's own file are rewritten in place (paragraph and run
   formatting preserved, words bold in the source bullet stay bold, project
   technologies optionally bold), extra bullets are removed, new ones are
   cloned from an existing bullet. Omitted entries: education is kept, jobs
   keep their title line, other entries are removed, and empty section
   headings are dropped. Facts that are their own entry line (degrees) are
   shown as the line, not duplicated as bullets. Anything the editor cannot
   preserve falls back to a clean reconstructed layout, labeled in the UI.
   PDF masters always use the reconstructed layout.
4. **No hidden omissions.** Per-entry bullet limits from the formatting
   preferences are applied to the content before rendering and reported
   (`capped_by_preference`). One page is reached only by dropping the least
   relevant bullets (fewest requirement matches, later entries first, at
   least one bullet per entry kept) for up to 6 rounds, reported in
   `dropped_to_fit`; fonts and margins are never shrunk. If it still does not
   fit, checks fail with an actionable message.
5. **Checks on the exact PDF.** After LibreOffice conversion (sandboxed
   container, at most 2 concurrent conversions per process), the PDF is
   inspected: one page, selectable text, no clipping, the header/contact
   lines, each entry line, dates, bullets, and skills present and in reading
   order within each group.
6. **Artifacts and approval.** Each render stores the PDF and internal DOCX in
   a private `artifacts` bucket (`<user_id>/<draft_version_id>/<artifact_id>`,
   per-owner Storage policies) and an insert-only `artifacts` row with the
   SHA-256, page count, layout, and checks. Approval requires the current
   draft version, the matching artifact hash, and passing checks; it inserts
   an immutable `approvals` row and moves the run to `approved`. Any later
   version returns the run to review; the earlier approved PDF stays
   downloadable. Downloads re-verify the stored bytes against the hash.
7. **Names.** `Prefix_Position_Company_CV.pdf`, every part reduced to ASCII
   letters/digits joined by underscores; prefix from the profile's
   `filename_prefix`, else the display name without spaces, else `Resume`.
8. **Preview.** The PDF route is the only response our own pages may frame
   (`frame-ancestors 'self'`, `X-Frame-Options: SAMEORIGIN`); the UI shows a
   PNG of page 1 rendered from the same stored, hash-checked bytes plus an
   "Open the PDF" link.
9. **Preferences with consent.** Formatting choices live in each draft version
   (so approval covers them). They are stored on the profile only when the
   user ticks "remember" or saves defaults in Settings; new drafts start from
   the remembered defaults.

## Verified (2026-10-11)
`tests/test_review_flow.py` (7 integration tests with real rendering):
render -> preview (PNG and framable PDF) -> wrong-hash approval refused ->
approval -> named download with matching hash; unsupported edits rejected;
valid edit creates a version, clears approval, stale approval refused, old
approval still downloadable; restore; move/remove/add-fact/format edits;
remembered preferences applied to new drafts; regeneration keeps entry refs;
demo chat makes no changes; scripted chat patch strips "60%"/"Snowflake";
long resume fit to one page by dropping bullets; PDF master uses the labeled
reconstructed layout; all review routes 404 for another user and Storage
listing of another user's folder is empty. Manual browser run: PDF created
from a PDF master (reconstructed), preview shown, approved, downloaded bytes
hash-verified, unsupported "35%" edit rejected; found and fixed horizontal
overflow from long filenames at phone width. Real-model chat/regeneration
quality is untested (no API key), as for milestone 4.

## Limitations
- Rendering and conversions run inside the request (up to about a minute
  with several fitting rounds); milestone 6 moves them to the worker.
- Template rendering handles body paragraphs; complex layouts (tables used as
  columns, text boxes) fall back to the reconstructed layout.
- Version comparison is bullet-level set difference, not a word diff.
- The reconstructed layout uses a single built-in design.
