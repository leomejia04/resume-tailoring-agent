"""Render a draft to the exact PDF the user will approve, and check it.

Template-aware rendering is used for DOCX masters when the layout allows it;
otherwise the reconstructed layout is used and labeled. One page is reached
only by dropping the least relevant bullets (bounded rounds), never by
shrinking fonts or margins. The final PDF is inspected for page count,
clipping, selectable text, contact details, dates, and reading order.
"""

from __future__ import annotations

import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.documents.convert import docx_to_pdf
from app.documents.docx_edit import UnsupportedLayoutError
from app.documents.pdf_inspect import inspect_pdf, normalize
from app.render.format import FormatPreferences, is_entry_line
from app.render.reconstructed_docx import ExtraEntry, render_reconstructed
from app.render.template_docx import FactLocation, render_template
from app.tailoring.models import DraftContent

MAX_FIT_ROUNDS = 6
# LibreOffice conversions are CPU heavy; bound how many run at once per process.
_CONVERSIONS = threading.BoundedSemaphore(2)


class RenderChecks(BaseModel):
    passed: bool
    layout: Literal["template", "reconstructed"]
    page_count: int
    page_size_pt: tuple[float, float]
    searchable: bool
    clipped: list[str]
    missing_phrases: list[str]
    out_of_order: list[str]
    fonts: list[str]
    dropped_to_fit: list[str] = Field(default_factory=list)
    # Bullets beyond the per-entry limits in the formatting preferences.
    capped_by_preference: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    rounds: int


class RenderOutcome(BaseModel):
    pdf: bytes
    docx: bytes
    checks: RenderChecks
    content: DraftContent  # after any bullets dropped to fit


def _convert(docx: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="render-") as work, _CONVERSIONS:
        source = Path(work) / "draft.docx"
        source.write_bytes(docx)
        return docx_to_pdf(source, Path(work) / "draft.pdf").read_bytes()


def _phrase_groups(header_lines: list[str], content: DraftContent) -> list[list[str]]:
    groups = [[line for line in header_lines[:2] if line]]
    for entry in content.entries:
        group = [p for p in (entry.entry, entry.date_text) if p]
        group += [b.text for b in entry.bullets if not is_entry_line(b.text, entry.entry)]
        groups.append(group)
    groups.append([s.text for s in content.skills])
    return [g for g in groups if g]


def _check_phrases(text: str, groups: list[list[str]]) -> tuple[list[str], list[str]]:
    haystack = normalize(text)
    missing, out_of_order = [], []
    for group in groups:
        cursor = 0
        for phrase in group:
            position = haystack.find(normalize(phrase))
            if position == -1:
                missing.append(phrase[:80])
            elif position < cursor:
                out_of_order.append(phrase[:80])
            else:
                cursor = position
    return missing, out_of_order


def apply_caps(content: DraftContent, prefs: FormatPreferences) -> tuple[DraftContent, list[str]]:
    """Apply per-entry bullet limits to the content itself, so nothing is hidden at render time."""
    data = content.model_copy(deep=True)
    capped: list[str] = []
    for entry in data.entries:
        bullets = [b for b in entry.bullets if not is_entry_line(b.text, entry.entry)]
        cap = prefs.project_bullets if entry.section == "projects" else prefs.max_bullets_per_entry
        capped += [b.text[:120] for b in bullets[cap:]]
        keep = {id(b) for b in bullets[:cap]}
        entry.bullets = [
            b for b in entry.bullets if id(b) in keep or is_entry_line(b.text, entry.entry)
        ]
    return data, capped


def _drop_one(content: DraftContent) -> tuple[DraftContent, str] | None:
    """Remove the least relevant bullet, keeping at least one bullet per entry."""
    best = None
    for e_index in range(len(content.entries) - 1, -1, -1):
        entry = content.entries[e_index]
        if len(entry.bullets) < 2:
            continue
        for b_index in range(len(entry.bullets) - 1, -1, -1):
            score = len(entry.bullets[b_index].requirement_ids)
            if best is None or score < best[0]:
                best = (score, e_index, b_index)
    if best is None:
        return None
    _, e_index, b_index = best
    data = content.model_copy(deep=True)
    removed = data.entries[e_index].bullets.pop(b_index)
    return data, removed.text


def render_draft(
    *,
    master: bytes | None,
    master_kind: str | None,
    header_lines: list[str],
    content: DraftContent,
    facts: dict[uuid.UUID, FactLocation],
    prefs: FormatPreferences,
    extras: list[ExtraEntry],
    convert: Callable[[bytes], bytes] = _convert,
) -> RenderOutcome:
    notes: list[str] = []
    layout: Literal["template", "reconstructed"] = "reconstructed"
    if master is not None and master_kind == "docx":
        try:
            render_template(master, content, facts, prefs)
            layout = "template"
        except UnsupportedLayoutError as exc:
            notes.append(
                f"Your master's layout could not be edited safely ({exc}); "
                "a reconstructed layout was used instead. Review it before approving."
            )
    elif master_kind == "pdf":
        notes.append("PDF masters cannot be edited in place; a reconstructed layout was used.")

    def build(draft: DraftContent) -> tuple[bytes, list[str]]:
        if layout == "template" and master is not None:
            result = render_template(master, draft, facts, prefs)
            return result.docx, result.header_lines
        return render_reconstructed(header_lines, draft, prefs, extras), header_lines

    current, capped = apply_caps(content, prefs)
    if capped:
        notes.append(f"{len(capped)} bullets beyond your per-entry limits were left out.")
    dropped: list[str] = []
    for round_number in range(1, MAX_FIT_ROUNDS + 1):
        docx, header = build(current)
        pdf = convert(docx)
        with tempfile.TemporaryDirectory(prefix="inspect-") as work:
            path = Path(work) / "draft.pdf"
            path.write_bytes(pdf)
            report = inspect_pdf(path)
        if report.page_count > 1:
            step = _drop_one(current)
            if step is not None and round_number < MAX_FIT_ROUNDS:
                current, text = step
                dropped.append(text[:120])
                continue
            notes.append(
                "The resume does not fit on one page even after removing "
                f"{len(dropped)} lower-priority bullets. Remove or shorten content and try again."
            )
        missing, out_of_order = _check_phrases(report.text, _phrase_groups(header, current))
        checks = RenderChecks(
            passed=(
                report.page_count == 1
                and report.searchable
                and not report.clipped_spans
                and not missing
                and not out_of_order
            ),
            layout=layout,
            page_count=report.page_count,
            page_size_pt=report.page_size_pt,
            searchable=report.searchable,
            clipped=report.clipped_spans[:20],
            missing_phrases=missing[:20],
            out_of_order=out_of_order[:20],
            fonts=report.fonts[:20],
            dropped_to_fit=dropped,
            capped_by_preference=capped,
            notes=notes,
            rounds=round_number,
        )
        return RenderOutcome(pdf=pdf, docx=docx, checks=checks, content=current)
    raise AssertionError("unreachable")  # pragma: no cover
