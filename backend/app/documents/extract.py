"""Deterministic extraction of reviewable fact candidates from a master resume.

Every candidate starts unverified: extraction proposes, the user confirms.
Suggested experience types are hints only. Numbers found in the text are
recorded so later checks can tell supported metrics from invented ones.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pdfplumber
from docx import Document

from app.documents.docx_edit import body_paragraphs, is_bullet

MAX_CANDIDATES = 300
MAX_TEXT = 2000

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
_BULLET_GLYPH = re.compile(r"^\s*[•●▪◦‣\-\*–]\s+")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_RANGE = re.compile(
    rf"((?:{_MONTH}\s+)?(?:19|20)\d{{2}}\s*[-–—to]+\s*(?:(?:{_MONTH}\s+)?(?:19|20)\d{{2}}|present|current|now))"
    rf"|((?:{_MONTH}\s+)?(?:19|20)\d{{2}})",
    re.I,
)
_METRIC = re.compile(
    r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?\s?(?:%|percent|k|m|million|billion|x|\+)?(?![\w])", re.I
)


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


@dataclass
class ExtractionResult:
    candidates: list[Candidate]
    notes: list[str]
    status: str  # complete | partial | failed
    # Name/contact lines before the first section heading (identity, not evidence).
    header_lines: list[str] = field(default_factory=list)


def classify_heading(text: str) -> str | None:
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
        self.entry_index = None

    def entry_line(self, line: str) -> None:
        self._close_entry()
        self.entry, self.dates = split_entry(line)
        self.entry = self.entry[:300]
        self.entry_index = self.cursor
        self.entry_used = False

    def finish(self) -> None:
        self._close_entry()

    def add(self, text: str, paragraph_index: int | None = None) -> None:
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
            )
        )


def extract_docx(data: bytes) -> ExtractionResult:
    document = Document(io.BytesIO(data))
    builder = _Builder()
    notes: list[str] = []
    header: list[str] = []
    for index, paragraph in enumerate(body_paragraphs(document)):
        text = paragraph.text.strip()
        if not text:
            continue
        builder.cursor = index
        section = None if is_bullet(document, paragraph) else classify_heading(text)
        if section:
            builder.heading(section)
        elif builder.section is None:
            header.append(text[:300])  # name and contact lines are identity, not evidence
            continue
        elif is_bullet(document, paragraph) or builder.section in _LIST_SECTIONS:
            builder.add(text)
        else:
            builder.entry_line(text)
    builder.finish()
    if len(builder.candidates) >= MAX_CANDIDATES:
        notes.append(f"Only the first {MAX_CANDIDATES} items were extracted.")
    if not builder.candidates:
        notes.append(
            "No bullet points or section headings were recognized. "
            "Check that the resume uses standard headings."
        )
        return ExtractionResult([], notes, "partial", header[:6])
    return ExtractionResult(
        builder.candidates, notes, "partial" if notes else "complete", header[:6]
    )


def extract_pdf(data: bytes) -> ExtractionResult:
    builder = _Builder()
    notes = [
        "PDF layout was reconstructed from text positions; "
        "check that bullet points were split correctly."
    ]
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        lines: list[dict] = []
        for page in pdf.pages:
            lines.extend(page.extract_text_lines(return_chars=False))
    if not any(line["text"].strip() for line in lines):
        return ExtractionResult(
            [],
            [
                "No selectable text was found. "
                "Scanned resumes need text recognition, which is not available yet."
            ],
            "failed",
        )

    current: list[str] = []
    header: list[str] = []
    bullet_x: float | None = None

    def flush() -> None:
        nonlocal current
        if current:
            builder.add(" ".join(current))
            current = []

    for line in lines:
        text = line["text"].strip()
        if not text:
            continue
        is_bullet_line = bool(_BULLET_GLYPH.match(text))
        section = None if is_bullet_line else classify_heading(text)
        if section:
            flush()
            builder.heading(section)
        elif builder.section is None:
            header.append(text[:300])
            continue
        elif is_bullet_line:
            flush()
            current = [_BULLET_GLYPH.sub("", text)]
            bullet_x = line["x0"]
        elif current and bullet_x is not None and line["x0"] > bullet_x + 2:
            current.append(text)  # wrapped continuation of the previous bullet
        elif builder.section in _LIST_SECTIONS:
            flush()
            builder.add(text)
        else:
            flush()
            builder.entry_line(text)
    flush()
    builder.finish()
    if not builder.candidates:
        notes.append("No bullet points were recognized.")
    return ExtractionResult(builder.candidates, notes, "partial", header[:6])


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
