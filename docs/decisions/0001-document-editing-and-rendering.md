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
6. **Rendering with headless LibreOffice**, through two backends with one
   contract (`convert.py`). `local` runs an installed `soffice` in a fresh temp
   directory with a throwaway profile (used inside the production worker
   container). `docker` streams the DOCX to a disposable container on stdin and
   reads the PDF from stdout (used on development hosts): no network, no host
   mounts, read-only root filesystem with a private tmpfs, non-root user, all
   capabilities dropped, no-new-privileges, 1 GB memory, 1 CPU, 256 processes,
   a timeout inside the container, and an outer timeout that force-removes the
   container. Both use argument lists (no shell), fixed filenames, a 20 MiB
   input cap, and require output starting with `%PDF-`. The host machine is
   never assumed to have Word or LibreOffice.
   The image (`docker/converter/`) is Debian 13 slim pinned by digest with
   LibreOffice 25.2.3 (`libreoffice-writer-nogui`) and Liberation, Carlito,
   Caladea, and DejaVu fonts. Debian package versions are recorded in
   `/etc/converter-versions.txt` rather than pinned, because Debian drops
   superseded versions from its mirrors; rebuilds pick up security updates.
7. **PDF inspection with pdfplumber** (pdfminer.six text/positions, pypdfium2 page images): page count, page size, searchable text,
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
- The `local` backend on Windows is unverified: a timeout kills the launcher,
  and a stray `soffice.bin` process can survive. Development uses the Docker
  backend; production runs `local` inside a Linux container.
- Verified result (2026-10-11): the fictional master and edited copy both
  render to one US Letter page with no clipping, all expected phrases in
  order, and fonts LiberationSans/LiberationSans-Bold (Arial substitute) plus
  OpenSymbol (bullet glyphs).

## Licensing note

PyMuPDF was originally specified but is dual-licensed AGPL-3.0 / commercial,
which can impose source-availability obligations on a network service. The
project owner chose a permissive stack instead (approved 2026-10-11):
pdfplumber (MIT), pdfminer.six (MIT), pypdfium2 (BSD-3-Clause/Apache-2.0,
wrapping Google's PDFium), and Pillow (MIT-CMU). python-docx is MIT;
LibreOffice (MPL-2.0) runs as a separate process. Check licenses again when
adding OCR or other document dependencies.
