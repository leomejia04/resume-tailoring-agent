# ADR 0011: One standard resume layout

Status: accepted (2026-10-12, owner request).

## Context

The owner asked that every finished resume use the format of their own
reference resume, with sections varying by master but always in that format,
and scoring as high as possible on the ATS match estimate. Until now a DOCX
master was edited in place (keeping its design) and a PDF master got a plain
"reconstructed" layout (Arial, one-line entry headers).

The reference is a conventional single-column, one-page resume, which is
also what applicant tracking systems parse most reliably: no tables, text
boxes, columns, or graphics; standard section names; dates and locations as
text.

Reading the reference exposed an extraction gap: two-line entry headers
("Organization<TAB>Location" over "Title<TAB>Dates") were split into two
entries, so the organization became its own "fact" and the title lost its
employer. The same happened to education (school, then degree, then a
"Relevant Coursework:" line).

## Decisions

1. **The standard layout is the default for every resume.** Measured from
   the reference (pdfplumber on the PDF Word produced): US Letter; Times New
   Roman (Liberation Serif in the converter, metric-compatible); 1.6 cm side
   and 1.4 cm top/bottom margins; name 17.5 pt bold centered; contact line
   9.5 pt centered with real hyperlinks; section headings 11.5 pt bold
   capitals with 15 pt above and a 1 pt full-width rule; body 9.5 pt on an
   exact 11.75 pt line pitch; entries as bold organization with right-aligned
   location over title with right-aligned dates (education titles bold, job
   titles regular), or one bold line with dates for single-line entries such
   as projects; a small round bullet at 0.125 in with text at 0.25 in; bold
   label lines ("Relevant Coursework:", skills categories). Section order:
   Summary, Education, Work Experience, Projects, Leadership &
   Extra-Curricular Activities, Certifications, Awards, Skills; a section
   appears only when the draft has content for it.
2. **Keeping a Word master's own design stays available** as a formatting
   preference (`layout: "master"`, "Keep my Word file's own design" in the
   UI). PDF masters always use the standard layout. Artifacts record
   `layout = "standard" | "template"`; old `"reconstructed"` artifacts stay
   valid history.
3. **Entry headers are stored as parts.** Extraction now splits a
   right-aligned segment (a tab in DOCX, a gap wider than 15 pt between words
   in PDF) into location or dates, and merges a header line with the next
   line when the pair forms a two-line header (the second has dates or one
   of them has a location; never two dated lines, never after a line that
   already has bullets). The entry label becomes "Title | Organization".
   `evidence_versions` gains `organization`, `location`, `title` (migration
   `0009`): protected like `entry` and `date_text`, copied from the master
   or from the chosen entry for confirmed answers, never written by the
   model. A "Label: text" line under an entry is a fact of that entry.
4. **Emphasis is kept.** Terms the master set in bold inside a bullet are
   stored per fact (`bold_terms`, at most 30); a rewording keeps emphasis
   only for terms still present. In the standard layout a bullet bolds its
   cited facts' terms, and project bullets also bold technologies the master
   bolded in any project (the existing "bold project technologies"
   preference); "Python, Pandas, and NumPy" becomes one bold run, as in the
   reference.
5. **Header links are kept.** The URL behind header text such as "LinkedIn"
   is read from the master (http, https, or mailto only; at most 500
   characters) and written back as a real hyperlink. It is re-read from the
   master at render time, so nothing new is stored.
6. **Checks.** The existing one-page, clipping, searchable-text, and reading
   order checks apply unchanged, and now know the two-line header order
   (organization, location, title, dates). Two checker fixes found while
   building this: reading order searches forward from the previous phrase
   (a city or date can repeat across entries), and a word hyphenated across
   a line break ("top-" / "performing") is compared as one word.

## "Highest ATS score possible"

The layout itself is ATS-safe: one column, real text, standard headings,
explicit dates. The score comes from content: the drafting prompt maps
confirmed facts to the posting's requirements, retrieval keeps the
best-supported facts (ADR 0010), and up to two automatic improvement
attempts add requirements the confirmed facts support but the draft missed
(ADR 0008). The score is never raised by adding unconfirmed skills or
repeating keywords: the rubric gives no credit for repetition, and
truthfulness rules forbid unsupported claims. Unsupported requirements are
reported as gaps with questions, so the user can confirm real experience.

One trade-off is left as in the reference: the contact line shows the word
"LinkedIn" as a link. Some ATS ignore link targets, so a visible URL would be
read more reliably; it is the owner's call.

## Verification

- The owner's reference PDF, read by the new extraction and rendered in the
  standard layout (locally, not stored): one page, all checks passed, fonts
  Liberation Serif regular/bold, headings within 0-7 pt of the reference's
  positions and the last line 11 pt lower (skills lines are 0.75 pt tighter
  in the reference); visually the same structure, emphasis, rules, link, and
  bullets.
- `tests/test_standard_layout.py` (10, fictional data): DOCX and PDF round
  trips recover organization, location, title, dates, coursework, bold terms
  and the link; typography (margins, sizes, rule, bullet glyph, hyperlink);
  emphasis joining; single-line entries never merged; safe links only;
  reading-order and hyphenation checker fixes.
- Updated flow tests: a DOCX master now renders `standard` by default and
  `template` when the user keeps the Word design; a PDF master renders
  `standard` without a fallback note. Migration 0009 round trip and
  `alembic check` clean.

## Consequences

- Masters uploaded before this change keep their old split entries; the
  standard layout still renders them (as single-line entries), and
  re-uploading the master fills the new parts.
- The renderer writes one fixed design; other designs would be new layouts
  with their own measurements and tests.
