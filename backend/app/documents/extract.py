"""Deterministic extraction of reviewable fact candidates from a master resume.

Every candidate starts unverified: extraction proposes, the user confirms.
Suggested experience types are hints only. Numbers found in the text are
recorded so later checks can tell supported metrics from invented ones.

Entry headers may take one line ("Analyst | Co.<TAB>Jun 2025 - Aug 2025") or
two ("Co.<TAB>City, ST" then "Analyst<TAB>Jun 2025 - Aug 2025"); a right-aligned
segment (a tab in DOCX, a wide gap in PDF) is a location or dates. The parts
are kept (organization, location, title) so the standard layout can show them
as the master did (ADR 0011). Terms the master set in bold inside a bullet
are kept too.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import pdfplumber
from docx import Document

from app.documents.docx_edit import body_paragraphs, is_bullet

MAX_CANDIDATES = 300
MAX_TEXT = 2000
MAX_BOLD_TERMS = 30

_SECTIONS = {
    "summary": ("summary", "profile", "objective", "about"),
    "experience": ("experience", "employment", "work history", "professional experience"),
    "education": ("education", "academic"),
    "projects": ("projects", "project experience", "portfolio"),
    "skills": ("skills", "technical skills", "technologies", "tools"),
    "certifications": ("certifications", "certificates", "licenses"),
    "leadership": ("leadership", "activities", "volunteer", "involvement", "extracurricular"),
    "awards": ("awards", "honors", "achievements"),
}
_LIST_SECTIONS = {"skills", "certifications", "awards", "summary"}
_BULLET_GLYPH = re.compile(r"^\s*[•●▪◦‣\-\*–]\s+")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_RANGE = re.compile(
    rf"((?:{_MONTH}\s+)?(?:19|20)\d{{2}}\s*[-–—to]+\s*(?:(?:{_MONTH}\s+)?(?:19|20)\d{{2}}|present|current|now))"
    rf"|((?:{_MONTH}\s+)?(?:19|20)\d{{2}})",
    re.I,
)
_METRIC = re.compile(
    r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?\s?(?:%|percent|k|m|million|billion|x|\+)?(?![\w])", re.I
)
# "Relevant Coursework: ...": a labeled detail line under an entry, not a new entry.
_LABEL_LINE = re.compile(r"^[A-Z][A-Za-z0-9&/(),.' -]{1,40}:\s+\S")
_TERM_SPLIT = re.compile(r"\s*,\s*(?:and\s+)?|\s+and\s+")
# Horizontal gap (points) between words that marks a right-aligned PDF segment.
_PDF_SEGMENT_GAP = 15
_LINK_SCHEMES = {"http", "https", "mailto"}


@dataclass
class Candidate:
    text: str
    section: str | None
    entry: str | None
    date_text: str | None
    position: int
    metrics: list[str] = field(default_factory=list)
    suggested_type: str = "unclassified"
    # DOCX only: where the fact and its entry line live (used by the renderer).
    paragraph_index: int | None = None
    entry_paragraph_index: int | None = None
    organization: str | None = None
    location: str | None = None
    title: str | None = None
    bold_terms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HeaderLink:
    text: str
    url: str


@dataclass
class ExtractionResult:
    candidates: list[Candidate]
    notes: list[str]
    status: str  # complete | partial | failed
    # Name/contact lines before the first section heading (identity, not evidence).
    header_lines: list[str] = field(default_factory=list)
    # Links behind header text (e.g. "LinkedIn"), http(s)/mailto only.
    header_links: list[HeaderLink] = field(default_factory=list)


def classify_heading(text: str) -> str | None:
    if ":" in text and text.split(":", 1)[1].strip():
        return None  # "Tools: Excel" is a skills line, not a heading
    cleaned = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if not cleaned or len(cleaned) > 40:
        return None
    for section, words in _SECTIONS.items():
        if any(
            cleaned == w or cleaned.startswith(w + " ") or cleaned.endswith(" " + w) for w in words
        ):
            return section
    return None


def find_metrics(text: str) -> list[str]:
    found = []
    for match in _METRIC.finditer(text):
        value = match.group(0).strip()
        # Years inside date ranges are dates, not metrics.
        if re.fullmatch(r"(19|20)\d{2}", value):
            continue
        found.append(value)
    return found


def split_entry(line: str) -> tuple[str, str | None]:
    """Split a title line from its dates, e.g. "Analyst | Co.<TAB>Jun 2025 - Aug 2025"."""
    match = None
    for m in _DATE_RANGE.finditer(line):
        match = m
    if match is None:
        return line.strip(), None
    entry = (line[: match.start()] + line[match.end() :]).strip(" \t|,-–")
    return entry or line.strip(), match.group(0).strip()


def is_dates(text: str) -> bool:
    """True when the text is (almost) only a date or date range."""
    stripped = text.strip()
    match = _DATE_RANGE.search(stripped)
    return match is not None and len(match.group(0)) >= len(stripped) - 2


def split_bold_terms(phrases: list[str]) -> list[str]:
    """'Python, Pandas, and NumPy' -> ['Python', 'Pandas', 'NumPy'] (deduplicated)."""
    terms: list[str] = []
    for phrase in phrases:
        for part in _TERM_SPLIT.split(phrase):
            term = part.strip(" ,;:.()")
            if 0 < len(term) <= 60 and re.search(r"[A-Za-z]", term) and term not in terms:
                terms.append(term)
    return terms[:MAX_BOLD_TERMS]


def safe_link(url: str | None) -> str | None:
    if not url or len(url) > 500:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in _LINK_SCHEMES:
        return None
    if parts.scheme.lower() != "mailto" and not parts.netloc:
        return None
    return url.strip()


def suggest_type(section: str | None, entry: str | None) -> str:
    entry_lower = (entry or "").lower()
    if section == "experience":
        return "internship" if re.search(r"\bintern(ship)?\b", entry_lower) else "professional"
    if section == "education":
        return "academic"
    if section in {"projects", "leadership"}:
        return "personal"
    return "unclassified"


class _Builder:
    def __init__(self) -> None:
        self.section: str | None = None
        self.entry: str | None = None
        self.dates: str | None = None
        self.organization: str | None = None
        self.location: str | None = None
        self.title: str | None = None
        self.entry_used = True
        self.candidates: list[Candidate] = []
        self.cursor: int | None = None  # paragraph index of the line being processed
        self.entry_index: int | None = None

    def _close_entry(self) -> None:
        # An entry line with nothing under it (e.g. a degree) is itself a fact.
        if self.entry and not self.entry_used:
            self.add(self.entry, paragraph_index=self.entry_index)
        self.entry_used = True

    def heading(self, section: str) -> None:
        self._close_entry()
        self.section, self.entry, self.dates = section, None, None
        self.organization = self.location = self.title = None
        self.entry_index = None

    def entry_line(self, text: str, right: str | None = None) -> None:
        text = re.sub(r"\s+", " ", text).strip()
        if self.entry and not right and _LABEL_LINE.match(text):
            self.add(text)  # "Relevant Coursework: ..." belongs to the current entry
            return
        label, dates = split_entry(text)
        location = None
        if right:
            if is_dates(right):
                dates = right.strip()
            else:
                location = right.strip()[:100]
        if (
            self.entry
            and not self.entry_used
            and self.organization is None
            and not (self.dates and dates)
            and (dates or location or self.location)
        ):
            # Second line of a two-line header: organization above, title below.
            self.organization, self.title = self.entry[:200], label[:200]
            self.location = self.location or location
            self.dates = self.dates or dates
            self.entry = f"{label} | {self.organization}"[:300]
            return
        self._close_entry()
        self.entry, self.dates, self.location = label[:300], dates, location
        self.organization = self.title = None
        self.entry_index = self.cursor
        self.entry_used = False

    def finish(self) -> None:
        self._close_entry()

    def add(
        self, text: str, paragraph_index: int | None = None, bold: list[str] | None = None
    ) -> None:
        self.entry_used = True
        text = re.sub(r"\s+", " ", text).strip()
        if not text or len(self.candidates) >= MAX_CANDIDATES:
            return
        self.candidates.append(
            Candidate(
                text=text[:MAX_TEXT],
                section=self.section,
                entry=self.entry,
                date_text=(self.dates or "")[:100] or None,
                position=len(self.candidates),
                metrics=find_metrics(text),
                suggested_type=suggest_type(self.section, self.entry),
                paragraph_index=paragraph_index if paragraph_index is not None else self.cursor,
                entry_paragraph_index=self.entry_index,
                organization=self.organization,
                location=self.location,
                title=self.title,
                bold_terms=[t for t in split_bold_terms(bold or []) if t in text],
            )
        )


def _docx_bold_phrases(paragraph) -> list[str]:  # noqa: ANN001
    full = paragraph.text.strip()
    phrases, current = [], ""
    for run in paragraph.runs:
        if run.bold and run.text.strip():
            current += run.text
        else:
            if current.strip() and current.strip() != full:
                phrases.append(current.strip())
            current = ""
    if current.strip() and current.strip() != full:
        phrases.append(current.strip())
    return phrases


def _docx_links(paragraph) -> list[HeaderLink]:  # noqa: ANN001
    links = []
    for link in paragraph.hyperlinks:
        url = safe_link(link.url)
        if url and link.text.strip():
            links.append(HeaderLink(link.text.strip()[:100], url))
    return links


def _tab_parts(text: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in re.split(r"\t+", text) if p.strip()]
    if len(parts) < 2:
        return text.strip(), None
    return " ".join(parts[:-1]), parts[-1]


def extract_docx(data: bytes) -> ExtractionResult:
    document = Document(io.BytesIO(data))
    builder = _Builder()
    notes: list[str] = []
    header: list[str] = []
    links: list[HeaderLink] = []
    for index, paragraph in enumerate(body_paragraphs(document)):
        text = paragraph.text.strip()
        if not text:
            continue
        builder.cursor = index
        bullet = is_bullet(document, paragraph)
        section = None if bullet else classify_heading(text)
        if section:
            builder.heading(section)
        elif builder.section is None:
            header.append(re.sub(r"\s+", " ", text)[:300])  # identity, not evidence
            links += _docx_links(paragraph)
            continue
        elif bullet or builder.section in _LIST_SECTIONS:
            builder.add(text, bold=_docx_bold_phrases(paragraph))
        else:
            builder.entry_line(*_tab_parts(text))
    builder.finish()
    if len(builder.candidates) >= MAX_CANDIDATES:
        notes.append(f"Only the first {MAX_CANDIDATES} items were extracted.")
    if not builder.candidates:
        notes.append(
            "No bullet points or section headings were recognized. "
            "Check that the resume uses standard headings."
        )
        return ExtractionResult([], notes, "partial", header[:6], links[:6])
    return ExtractionResult(
        builder.candidates, notes, "partial" if notes else "complete", header[:6], links[:6]
    )


@dataclass
class _PdfLine:
    text: str
    right: str | None
    x0: float
    top: float
    bottom: float
    bold: list[str]


def _is_bold(word: dict) -> bool:
    name = str(word.get("fontname", "")).lower()
    return any(k in name for k in ("bold", "black", "heavy", "semibold"))


def _join(words: list[dict]) -> str:
    """Words to text; pieces split only by a font change ("Pandas" + ",") touch."""
    text = ""
    for i, word in enumerate(words):
        if i and word["x0"] - words[i - 1]["x1"] > 0.5:
            text += " "
        text += word["text"]
    return text


def _pdf_lines(page) -> list[_PdfLine]:  # noqa: ANN001
    """Words grouped into visual lines, with a right-aligned segment split off."""
    words = page.extract_words(extra_attrs=["fontname", "size"], keep_blank_chars=False)
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(word["top"] - rows[-1][0]["top"]) <= 2:
            rows[-1].append(word)
        else:
            rows.append([word])
    lines = []
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        segments: list[list[dict]] = [[row[0]]]
        for word in row[1:]:
            if word["x0"] - segments[-1][-1]["x1"] > _PDF_SEGMENT_GAP:
                segments.append([word])
            else:
                segments[-1].append(word)
        main = [w for segment in segments[:-1] for w in segment] if len(segments) > 1 else row
        right = _join(segments[-1]) if len(segments) > 1 else None
        bold: list[str] = []
        current: list[dict] = []
        for word in main:
            if _is_bold(word) and not _BULLET_GLYPH.match(word["text"] + " "):
                current.append(word)
            elif current:
                bold.append(_join(current))
                current = []
        if current:
            bold.append(_join(current))
        text = _join(main)
        if bold and len(bold) == 1 and bold[0] == _BULLET_GLYPH.sub("", text):
            bold = []  # a fully bold line (a header) has no emphasized terms
        lines.append(
            _PdfLine(
                text,
                right,
                row[0]["x0"],
                min(w["top"] for w in row),
                max(w["bottom"] for w in row),
                bold,
            )
        )
    return lines


def _pdf_links(page, lines: list[_PdfLine]) -> list[HeaderLink]:  # noqa: ANN001
    links = []
    for link in page.hyperlinks:
        url = safe_link(link.get("uri"))
        if not url:
            continue
        words = page.crop(
            (link["x0"], link["top"], link["x1"], link["bottom"]), strict=False
        ).extract_words()
        text = " ".join(w["text"] for w in words).strip()
        on_header = any(line.top - 2 <= link["top"] <= line.bottom + 2 for line in lines)
        if text and on_header:
            links.append(HeaderLink(text[:100], url))
    return links


def extract_pdf(data: bytes) -> ExtractionResult:
    builder = _Builder()
    notes = [
        "PDF layout was reconstructed from text positions; "
        "check that bullet points were split correctly."
    ]
    header: list[str] = []
    links: list[HeaderLink] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(page, _pdf_lines(page)) for page in pdf.pages]
        if not any(line.text.strip() for _, lines in pages for line in lines):
            return ExtractionResult(
                [],
                [
                    "No selectable text was found. "
                    "Scanned resumes need text recognition, which is not available yet."
                ],
                "failed",
            )

        current: list[str] = []
        current_bold: list[str] = []
        bullet_x: float | None = None

        def flush() -> None:
            nonlocal current, current_bold
            if current:
                # "top-" wrapped onto "performing" is one word: "top-performing".
                joined = re.sub(r"(?<=[A-Za-z])- (?=[a-z])", "-", " ".join(current))
                builder.add(joined, bold=current_bold)
                current, current_bold = [], []

        for page_number, (page, lines) in enumerate(pages):
            header_lines: list[_PdfLine] = []
            for line in lines:
                text = line.text.strip()
                if not text:
                    continue
                full_text = f"{text} {line.right}" if line.right else text
                is_bullet_line = bool(_BULLET_GLYPH.match(text))
                section = None if is_bullet_line else classify_heading(full_text)
                if section:
                    flush()
                    builder.heading(section)
                elif builder.section is None:
                    header.append(full_text[:300])
                    header_lines.append(line)
                    continue
                elif is_bullet_line:
                    flush()
                    current, current_bold = [_BULLET_GLYPH.sub("", text)], list(line.bold)
                    bullet_x = line.x0
                elif current and bullet_x is not None and line.x0 > bullet_x + 2:
                    current.append(text)  # wrapped continuation of the previous bullet
                    current_bold += line.bold
                elif builder.section in _LIST_SECTIONS:
                    flush()
                    builder.add(full_text, bold=line.bold)
                else:
                    flush()
                    builder.entry_line(text, line.right)
            if page_number == 0 and header_lines:
                links = _pdf_links(page, header_lines)
        flush()
    builder.finish()
    if not builder.candidates:
        notes.append("No bullet points were recognized.")
    return ExtractionResult(builder.candidates, notes, "partial", header[:6], links[:6])


def plain_text(data: bytes, kind: str) -> str:
    """The master's selectable text line by line (for the ATS baseline), no structure."""
    if kind == "docx":
        return "\n".join(p.text for p in body_paragraphs(Document(io.BytesIO(data))))
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(
            line["text"]
            for page in pdf.pages
            for line in page.extract_text_lines(return_chars=False)
        )
