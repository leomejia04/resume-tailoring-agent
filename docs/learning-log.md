# Learning log

Short notes on concepts introduced while building, in plain language.

## Milestone 1: document proof

- **A DOCX is a ZIP of XML files.** `word/document.xml` holds the text;
  `styles.xml`, `numbering.xml` (bullets), and `theme` hold shared formatting.
  You can rename a `.docx` to `.zip` and look inside.
- **Paragraphs and runs.** A paragraph (`w:p`) has paragraph properties
  (`w:pPr`: bullet, indent, spacing, tab stops) and a list of runs (`w:r`). A run
  is a stretch of text sharing one formatting (`w:rPr`: font, size, bold).
  "Built **Python** pipelines" is three runs. Editing safely means changing runs
  while leaving `w:pPr` alone.
- **Why not just `paragraph.text = "..."`?** python-docx's setter drops all runs
  and creates one run with no formatting, so bolding and explicit fonts vanish.
- **Canonical XML (C14N)** is a standard way to serialize XML so two documents
  with the same meaning produce identical bytes. We compare canonical forms so
  harmless re-serialization does not look like a change.
- **Optimistic concurrency, in miniature.** The edit carries the text the caller
  last saw. If the document changed in between, the edit is rejected rather than
  applied to the wrong content. The same idea will protect approvals later
  (version numbers instead of text).
- **Subprocess safety.** Passing a list of arguments with `shell=False` means no
  shell parses the command, so a filename like `a.docx; rm -rf /` is just a
  strange filename, not a command. We go further and never pass user filenames.
- **Metric-compatible fonts.** Liberation Sans has the same character widths as
  Arial, so text wraps identically on Windows and in a Linux container. Font
  width differences are a common cause of "fits on one page locally, two pages
  in production".
- **Inspecting the real PDF.** We check the artifact users download, not the
  DOCX we intended: page count, extractable text (ATS parsers need it), text
  touching page edges (clipping), and phrase order (reading order).
- **Docker image vs container.** An image is a packaged, read-only environment
  ("Debian + LibreOffice + fonts"); a container is one running copy of it. We
  start a fresh container for every conversion and delete it afterwards, so
  nothing from one user's document can linger for the next.
- **Sandboxing a converter.** Office files are a classic attack vector, so the
  container gets no network, no access to your folders, a read-only system, no
  root user, and memory/CPU/process limits. Data goes in through stdin and the
  PDF comes out through stdout, like piping in a terminal: `cat a.docx | convert`.
- **Pinning by digest.** `debian:trixie-slim` is a moving tag; `@sha256:...` is
  a fingerprint of exact image contents, so builds are reproducible.
- **Line endings.** Windows uses CRLF, Linux LF. A CRLF in a shell script makes
  Linux look for `/bin/sh\r` (with a hidden carriage return), which does not exist; `.gitattributes` forces LF.
- **Tests that catch the tester.** The reading-order check failed the first
  real run because the test listed phrases in the wrong order. A check that
  can fail is worth more than one that always passes.
