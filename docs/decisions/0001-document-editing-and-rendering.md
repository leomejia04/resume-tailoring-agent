# ADR 0001: Template-aware DOCX editing and LibreOffice rendering

Status: accepted (milestone 1)

## Context

Tailored resumes must keep the user's own master layout (margins, fonts,
spacing, bullets, run-level bolding) and must be exported as a verified PDF.
Masters are immutable.

## Decision

1. **Edit text, not layout.** `app/documents/docx_edit.py` replaces the runs of
   one paragraph and never modifies its paragraph properties (`w:pPr`), so
   bullets, indents, tab stops, and spacing are preserved by construction. New
   runs clone run properties (`w:rPr`) that already exist in that paragraph;
   plain text clones a plain run, bold text clones a bold run. If the paragraph
   has no bold run, bold is derived from the plain run with only `w:b` added.
2. **Fail closed on unsupported structure.** Paragraphs containing hyperlinks,
   fields, tracked changes, content controls, drawings, embedded objects, or
   footnote references raise `UnsupportedLayoutError`. Text boxes are not
   listed as editable paragraphs.
3. **Optimistic edit check.** Callers pass the paragraph's expected current text;
   a mismatch raises `StaleEditError` instead of editing the wrong bullet.
4. **Masters are copied, never opened for writing.** `edit_copy` hashes the
   master before and after and refuses an output path equal to the master.
5. **Independent verification.** `docx_verify.py` compares the two packages
   using canonical XML (C14N): every non-document part must be identical, page
   setup must be identical, every other paragraph's formatting and runs must be
   identical, and the target's `w:pPr` must be identical.
6. **Rendering with headless LibreOffice** in a fresh temp directory with a
   throwaway profile, fixed input filename, argument list (no shell), and a
   timeout. Production rendering will run in a Linux container with pinned
   fonts; the host machine is never assumed to have Word or LibreOffice.
7. **PDF inspection with PyMuPDF**: page count, page size, searchable text,
   fonts used, spans touching the page edge (clipping), and expected phrases
   present in reading order.
8. **Fonts:** fixtures use Arial, which is metric-compatible with Liberation
   Sans (installable in the container), so line breaks and page fit match.
   Proprietary fonts such as Calibri need a metric-compatible substitute
   (Carlito) in the container; this must be verified per template.

## Limitations (reconstruction)

- **PDF masters cannot be edited in place.** A PDF has positioned glyphs, not
  paragraphs and runs. PDF uploads will be parsed for facts and rendered into a
  user-approved reconstructed template; exact visual preservation is not
  promised.
- **Unsupported DOCX structure** (text boxes, multi-column floating shapes,
  hyperlinks inside bullets, tracked changes, fields) is not rewritten. Such
  paragraphs need a template layout the user approves, or the content must
  stay locked.
- **Mixed formatting inside a run set** (e.g. italic + bold + colored words in
  one bullet) is reduced to the paragraph's plain and bold run formats. Other
  per-word formatting is not yet carried across rewrites.
- **Rendering fidelity is LibreOffice's, not Word's.** Minor differences in
  kerning and line breaking versus Microsoft Word are possible; the PDF we
  inspect and show is the PDF the user downloads.
- On Windows, `subprocess` timeouts kill the launcher; a stray `soffice.bin`
  process can survive. Production conversion runs in a container where the
  whole process tree is bounded.

## Licensing note

PyMuPDF is dual-licensed AGPL-3.0 / commercial (Artifex). Using it inside a
network service can trigger AGPL source-availability obligations. The PDF
inspection code is isolated in `pdf_inspect.py` so it can be swapped for a
permissively licensed library (e.g. pypdf or pdfminer.six) if the project
cannot accept AGPL terms. **Decision pending from the project owner.**
python-docx is MIT; LibreOffice (MPL-2.0) runs as a separate process.
