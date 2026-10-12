# ADR 0008: Bounded automatic ATS improvement

Status: accepted (milestone 6d).

## Context

The specification asks for at most two automatic improvement attempts per
run. They must stop once useful supported improvements are exhausted, and
must never chase an arbitrary threshold with fabrication or keyword stuffing.
A draft can miss a requirement that the user's confirmed facts do support:
the model left the fact out, or reworded it and dropped the posting's term.
The ATS rubric (ADR 0006) is deterministic, so Python, not the model, can
tell whether a change really helps.

## Decisions

1. **Targets come from the rubric, not the model.** `app/ats/improve.py`
   scores the current draft and, separately, each confirmed fact (as its own
   passage, with its experience type) using the same rubric. A target is a
   scored requirement where the facts would earn more credit than the draft.
   The same rules apply on both sides: whole terms, negation, and the
   professional-work cap. Requirements no confirmed fact supports are never
   targets, so the attempt cannot add unconfirmed skills. At most eight
   targets per attempt, must-haves first.
2. **A hard cap in the database.** `app.improvement_attempts` gets one row
   per attempt, inserted before any work or model call. `attempt BETWEEN 1
   AND 2` and a unique (run, attempt) pair make a third attempt impossible,
   even across job retries, worker restarts, two concurrent jobs, or a
   manual retry. A timed-out or interrupted attempt still uses its slot,
   because it may have cost money. An attempt refused by the spending limit
   also uses one (accepted; it is rare and errs on the side of fewer paid
   calls). The runtime role may update only the outcome columns.
3. **Concise patches, same checks.** Supporting facts that are skill lines
   are added word for word, with no model call. Entries holding supporting
   facts go to the model (`improve/v1`, `ImprovePatchOut`), together with
   only those entries' facts and current bullets and the targets with their
   supporting fact IDs: a section patch, never a full rewrite. The patch is
   merged through the same validation as chat and section rewrites. Bullets
   returned unchanged keep their requirement links.
4. **Strict acceptance.** An attempt is saved as a new version
   (`created_by = "improve"`, with its own ATS report) only if:
   - every patched bullet passed the fact checks (any removal rejects the
     whole attempt: `unsupported_changes`);
   - no entry grew by more than one bullet or beyond the per-entry maximum
     (`too_long`);
   - at least one requirement gained credit, none lost any, and the rounded
     estimate did not fall (`no_improvement`);
   - no term is repeated more often than before (`new_repetition`).
5. **Stopping.** After a saved attempt the next one targets only what is
   still supported and missing. Improvement stops, recording why, when:
   - nothing is below full credit (`no_gaps`);
   - no supported gap is left (`exhausted`);
   - an attempt was not saved;
   - the draft changed (`draft_changed`);
   - the draft could not be scored (`not_checked`);
   - both slots are used (`limit_reached`).

   A rejected attempt is not retried with the same targets.
6. **Automatic, durable chaining.** When a draft job succeeds, the
   transaction that records its success also queues the `improve` job
   (`FollowUp` in `queue.finish`). A crash therefore cannot keep the draft
   and lose the follow-up. If per-user limits refuse the follow-up, the
   draft still stands. `improve` runs in the workflow lane, so edits, chat,
   and rewrites wait for it (409 on the server; paused in the UI).
   `AUTO_IMPROVE=false` turns the automatic follow-up off. Users can always
   start it with `POST /runs/{id}/draft/improve` (202; checks the expected
   version and the remaining attempts; same rate limit as chat).
7. **Resumable and cancellable.** A retried job marks its own interrupted
   attempt as failed (`interrupted`) and continues from the last version it
   saved. Cancellation is checked between attempts and before saving, and
   `save_version` now refuses to write into a cancelled run. That closes the
   gap where a rewrite or chat job finishing after cancellation could still
   add a version.
8. **What the user sees.** `RunOut.improvements` lists each attempt:
   - status and outcome;
   - base and new version;
   - targeted requirement IDs;
   - scores before and after.

   `RunOut.improvements_left` gives the remaining attempts. The ATS panel
   explains each attempt and the stop reason in plain words, and offers
   "Try improving again" while attempts remain. Version history labels
   these versions "Automatic improvement".

## Verified

- Units (`tests/test_ats_improve.py`, 8):
  - supported versus unsupported targets;
  - a fact the draft reworded away;
  - negated and non-work facts;
  - skill-line support and priority;
  - the target cap;
  - strict acceptance: improvement, trade-offs, no change, repetition.
- Integration (`tests/test_improve_flow.py`, 5, inline runner):
  - an automatic attempt closes a supported gap and stops with `exhausted`
    without a second paid call;
  - the request to the model holds only the relevant entry and no personal
    details;
  - an unsupported "Tableau" suggestion is rejected and nothing is saved;
  - two timeouts use both slots, and the job's third attempt stops with
    `limit_reached` without calling the model;
  - a manual retry after edits works in demo mode, refuses a stale version
    (409) and another user (404), and returns 409 `improve_limit` when no
    attempts remain;
  - privileges: no cross-owner reads, no updates to targets or attempt
    numbers, no deletes, and the database refuses a third attempt.
- Frontend: `ImproveSection.test.tsx` (4), including that a stop reason
  about an older version is not shown after an edit. The job result records
  the draft version it checked.
- Live, in the browser with a real worker in demo mode (fictional local
  account):
  - a new draft queued `improve` by itself, 0.14 s after the draft job, and
    it stopped with `exhausted`, because demo drafts use every fact;
  - after the SQL bullet and the skills line were removed (estimate 14), a
    retry restored both word for word as version 4 (estimate 35);
  - edits that left SQL elsewhere on the page correctly used no attempt;
  - the second attempt saved version 7, after which the retry button
    disappeared;
  - worker logs held only IDs, kinds, and timings;
  - the layout fits 375 px without horizontal scrolling.

## Limitations

- Improvements may still be removed by one-page fitting when the PDF is
  rendered. The final-PDF check shows the real result, and fitting removals
  are reported.
- Live prompt quality for `improve/v1` is unmeasured. It ran only against
  scripted and demo stand-ins; any live run needs a new authorized budget.
- Targets rely on exact-term and shared-wording rules. Semantic support
  (synonyms) waits for milestone 7's measured retrieval.
