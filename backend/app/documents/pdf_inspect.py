"""Inspect a rendered PDF: page count, searchable text, clipping, fonts, order.

Uses pdfplumber (MIT, built on pdfminer.six) for text and character
positions, and its pypdfium2 backend (BSD-3/Apache-2.0) for page images.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

# Text closer than this to the page edge (points) is treated as clipped.
_EDGE_TOLERANCE = 1.0
# Embedded font subsets are named like "ABCDEF+ArialMT"; drop the prefix.
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


@dataclass
class PdfReport:
    page_count: int
    page_size_pt: tuple[float, float]
    text: str
    fonts: list[str]
    clipped_spans: list[str] = field(default_factory=list)
    missing_phrases: list[str] = field(default_factory=list)
    out_of_order_phrases: list[str] = field(default_factory=list)

    @property
    def searchable(self) -> bool:
        return bool(self.text.strip())

    def passes(self, max_pages: int = 1) -> bool:
        return (
            self.page_count <= max_pages
            and self.searchable
            and not self.clipped_spans
            and not self.missing_phrases
            and not self.out_of_order_phrases
        )


def normalize(text: str) -> str:
    """Collapse whitespace and unify bullet/quote characters for comparison."""
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")
    text = re.sub(r"[•●▪]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _touches_edge(word: dict, width: float, height: float) -> bool:
    return (
        word["x0"] < _EDGE_TOLERANCE
        or word["top"] < _EDGE_TOLERANCE
        or word["x1"] > width - _EDGE_TOLERANCE
        or word["bottom"] > height - _EDGE_TOLERANCE
    )


def inspect_pdf(path: Path, expected_phrases: list[str] | None = None) -> PdfReport:
    with pdfplumber.open(path) as pdf:
        texts, fonts, clipped = [], set(), []
        for page_number, page in enumerate(pdf.pages, start=1):
            texts.append(page.extract_text() or "")
            fonts.update(_SUBSET_PREFIX.sub("", c["fontname"]) for c in page.chars)
            for word in page.extract_words():
                if _touches_edge(word, page.width, page.height):
                    clipped.append(f"page {page_number}: {word['text'][:40]!r}")
        first = pdf.pages[0]
        report = PdfReport(
            page_count=len(pdf.pages),
            page_size_pt=(round(float(first.width), 1), round(float(first.height), 1)),
            text="\n".join(texts),
            fonts=sorted(fonts),
            clipped_spans=clipped,
        )

    haystack = normalize(report.text)
    cursor = 0
    for phrase in expected_phrases or []:
        needle = normalize(phrase)
        position = haystack.find(needle)
        if position == -1:
            report.missing_phrases.append(phrase)
            continue
        if position < cursor:
            report.out_of_order_phrases.append(phrase)
        cursor = max(cursor, position + len(needle))
    return report


def render_page_png(pdf_path: Path, png_path: Path, page_index: int = 0, dpi: int = 110) -> Path:
    with pdfplumber.open(pdf_path) as pdf:
        pdf.pages[page_index].to_image(resolution=dpi).save(png_path)
    return png_path
