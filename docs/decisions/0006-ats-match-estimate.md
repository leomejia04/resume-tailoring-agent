# ADR 0006: ATS match estimate rubric

Status: accepted for the scoring engine (milestone 6a). Report storage, the
approval gate, the durable worker, and automatic improvements follow in later
milestone 6 slices and will extend this record.

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

## Limitations

- Stemming and the stopword list are simple, and there are no synonyms
  (e.g., "MS Excel" vs "spreadsheets"). Semantic retrieval arrives with
  milestone 7 and must be measured before it influences scores.
- Years of experience are never computed or credited from dates.
- A master baseline has no experience context, so the academic-only cap can
  apply to a tailored report but not to its baseline.
- Passing our parser and rubric does not mean every ATS reads or ranks the
  PDF the same way. The UI must say this next to the number.
