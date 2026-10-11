"""Inspect a rendered PDF: page count, searchable text, clipping, fonts, order."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# Text closer than this to the page edge (points) is treated as clipped.
_EDGE_TOLERANCE = 1.0


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


def inspect_pdf(path: Path, expected_phrases: list[str] | None = None) -> PdfReport:
    with pymupdf.open(path) as pdf:
        texts, fonts, clipped = [], set(), []
        first = pdf[0].rect
        for page_number, page in enumerate(pdf, start=1):
            rect = page.rect
            texts.append(page.get_text("text", sort=True))
            for font in page.get_fonts():
                fonts.add(font[3])
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        x0, y0, x1, y1 = span["bbox"]
                        if not span["text"].strip():
                            continue
                        if (
                            x0 < rect.x0 + _EDGE_TOLERANCE
                            or y0 < rect.y0 + _EDGE_TOLERANCE
                            or x1 > rect.x1 - _EDGE_TOLERANCE
                            or y1 > rect.y1 - _EDGE_TOLERANCE
                        ):
                            clipped.append(f"page {page_number}: {span['text'][:40]!r}")
        report = PdfReport(
            page_count=pdf.page_count,
            page_size_pt=(round(first.width, 1), round(first.height, 1)),
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
    with pymupdf.open(pdf_path) as pdf:
        pdf[page_index].get_pixmap(dpi=dpi).save(png_path)
    return png_path
