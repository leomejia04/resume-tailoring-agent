# ADR 0006: ATS match estimate rubric

Status: accepted. Scoring engine (milestone 6a) and saved reports with the
approval gate (6b). The durable worker (6c) and automatic improvements (6d)
will extend this record.

## Context

Users want to know how well a resume covers a posting. Real applicant
tracking systems differ and are not public, so we can only offer an
**estimate from our own documented rubric**. It must be reproducible,
explainable requirement by requirement, impossible to raise by keyword
stuffing, and separate from factual validation and PDF readability checks.

## Decisions

1. **Deterministic Python, no model call.** `app/ats/` scores a job analysis
   (requirements with category, terms, mandatory flag) against passages of
   resume text. Arithmetic uses `fractions.Fraction`; only the displayed
   numbers are rounded (overall: half up to an integer). Version:
   `ats-rubric/v1`.
2. **Weights.** Required qualifications 50, responsibilities 25, preferred
   qualifications 15, relevant terminology 10. Categories the posting does
   not have are dropped and the rest renormalized (required + preferred only:
   50/65 and 15/65).
3. **Per-requirement credit.**
   | Status | Meaning | Credit |
   |---|---|---|
   | direct | every term appears affirmatively | 1 |
   | direct (partial) | some terms appear | share found, at least 1/2 |
   | related | validated draft link plus shared wording, or strong shared wording (>= 2 stems and >= half of the requirement's) | 1/2 |
   | missing | terms exist, none found | 0 |
   | uncertain | nothing to check deterministically, only negated mentions, or a draft link with no shared wording | 0 |

   Category score = mean credit. Uncertain items stay in the denominator so
   the estimate never rises because something could not be checked, and they
   are listed for the user to review. A report with nothing scoreable has no
   number (`score: null`), never 0.
4. **No reward for repetition.** Credit depends on presence per requirement,
   not frequency. Terms mentioned four or more times are listed so users see
   that repeating them does not help.
5. **Deduplication.** Requirements with the same terms (or the same text when
   they have no terms) merge into the most important category (required >
   responsibility > preferred > terminology); a merged requirement is
   mandatory if any duplicate was. One keyword cannot count in two categories.
6. **False-match defenses.**
   - Whole words or phrases only: Java does not match JavaScript; SQL does
     not match NoSQL. Phrase spacing and hyphens are flexible (Power BI,
     Power-BI, PowerBI); longer lowercase words accept a simple plural.
   - Terms of one or two characters and ordinary-word tool names (Excel,
     Go, Teams, Access, ...) must keep the posting's capitalization; one- and
     two-character terms must not be part of compounds (R&D, C++, C#,
     C-suite, go-to-market).
   - Negation is scoped to the clause (split at sentence punctuation, line
     breaks, bullets, pipes; not commas): a cue (no, not, without, never,
     lack, non, ...n't) directly before the term, optionally across up to
     three filler words ("no prior experience with SQL"), or "is not
     required" after it. "Not only SQL" is affirmative.
   - Posting-side negation: a term the posting mentions only negatively is
     excluded; a requirement left with no terms is not scored.
   - Context: when a requirement asks for professional experience (years,
     "professional", "industry", "work experience") and its terms appear only
     in academic or personal entries, credit is capped at related.
     Internships count as work; unknown context is not capped.
7. **Draft links are hints, not proof.** The drafting model's
   `requirement_ids` on a bullet count only when that bullet passed the
   truthfulness checks and shares wording with the requirement; otherwise the
   requirement is marked uncertain with a note. Links never produce direct
   credit.
8. **Passages describe what is shown.**
   - Draft reports score visible draft content: hidden headlines are skipped,
     entry-line facts (degrees) count once, and entries carry their
     experience type.
   - Final-PDF reports score the PDF's extracted text. Draft passages found in
     it keep their context and links (longest first, so a short passage
     cannot claim part of a bullet); bullets dropped to fit, or content a
     layout does not render, are absent. Remaining PDF text is scored as
     unattributed 40-word windows.
   - Master baselines score the master's text line by line (wrapped lines
     joined), without experience context.
9. **Comparisons are like-for-like.** `compare()` refuses reports with
   different rubric versions or different analysis fingerprints (SHA-256 of
   the requirement list), and lists requirements whose credit rose or fell.

## Saved reports and the approval gate (6b)

10. **One insert-only report per check.** `app.ats_reports` stores the owner,
    run, job analysis, kind (`master_baseline`, `draft`, `final_pdf`), exactly
    one subject (master version, draft version, or artifact; a check
    constraint enforces it), status (`pending`, `complete`, `failed`), score,
    result JSON, rubric version, analysis fingerprint, and an error code.
    Composite `(id, owner_id)` foreign keys make cross-owner references
    impossible; forced row-level security limits the runtime role to its own
    rows, with SELECT and INSERT only. Partial unique indexes allow at most one
    complete report per subject, analysis, and rubric version; inserts use
    `ON CONFLICT DO NOTHING`, so concurrent checks cannot store duplicates.
11. **When checks run.** Scoring is deterministic and takes milliseconds, so
    until the durable worker it runs inline, in the transaction that creates
    its subject:
    - the master baseline after the job analysis (the master is downloaded
      once and its text read line by line; `run_master` picks the run's
      chosen master, else the newest);
    - a draft report for every new version: generation, repair, direct edit,
      restore, section rewrite, and chat revision;
    - a final-PDF report when the PDF is rendered, scored from the text
      extracted from those exact bytes, in the same transaction as the
      artifact row.
    A scoring bug or unreadable file is stored as a failed report (logged by
    exception type only, never content) and never blocks saving a draft or
    starting a run.
12. **Staleness is derived, never written.** A report applies only to the
    run's current subject, the run's analysis, and the current rubric
    version. Otherwise the API shows `stale` (or `pending` when no report
    exists) and no number: a rubric upgrade, a new master version, or a PDF
    made before the latest draft change all surface this way without
    rewriting history.
13. **Approval requires a complete check of the exact PDF.** On top of the
    version + SHA-256 binding and the layout checks, `approve` returns 409
    (`ats_check_pending`, `ats_check_failed`, or `ats_check_stale`) unless the
    artifact has a complete final-PDF report under the current rubric. The
    score itself never blocks approval; must-have gaps are shown, not
    enforced.
14. **Explicit re-check.** `POST /runs/{id}/ats/check` (rate-limited, no model
    calls) scores whatever current subject lacks a complete report: it
    retries failures and re-checks after a rubric upgrade. Complete reports
    are never redone. GET routes never score.
15. **What the UI shows.** "ATS match estimate" with the rubric weights and
    the not-an-employer-score disclaimer beside the number; master, current
    draft, and final PDF side by side with the change versus the master; the
    category table; must-have gaps separately; direct, related, missing, and
    uncertain requirements with their passages (PDF passages labeled); and
    repeated terms with a note that repetition does not help. Pending,
    failed, and stale checks show words and a retry, never a number. Version
    history lists each version's estimate.

## Limitations

- Stemming and the stopword list are simple, and there are no synonyms
  (e.g., "MS Excel" vs "spreadsheets"). Semantic retrieval arrives with
  milestone 7 and must be measured before it influences scores.
- Years of experience are never computed or credited from dates.
- A master baseline has no experience context, so the academic-only cap can
  apply to a tailored report but not to its baseline.
- Passing our parser and rubric does not mean every ATS reads or ranks the
  PDF the same way. The UI says this next to the number.
- Match passages show where the resume text matched; they do not yet link
  back to the evidence record behind each bullet (the draft editor shows
  those sources).
- The baseline needs the master file from storage under the user's session.
  The durable worker (6c) must solve storage access without a service role.
