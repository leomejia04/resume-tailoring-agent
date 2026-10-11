"""Render a draft into a copy of the user's own DOCX master (template-aware).

Bullet paragraphs are rewritten in place with ``replace_paragraph_text`` so
paragraph and run formatting come from the master; extra bullets are removed
and new ones cloned from an existing bullet. Words that were bold in the
source bullet stay bold. Anything the editor cannot preserve raises
``UnsupportedLayoutError`` so the caller can fall back to the reconstructed
layout with a visible notice.
"""

from __future__ import annotations

import copy
import io
import re
import uuid
from dataclasses import dataclass, field

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from app.documents.docx_edit import (
    TextSegment,
    UnsupportedLayoutError,
    body_paragraphs,
    is_bullet,
    replace_paragraph_text,
)
from app.documents.extract import Candidate, classify_heading, extract_docx
from app.render.format import FormatPreferences, is_entry_line
from app.tailoring.models import DraftContent

KEEP_WHEN_OMITTED = {"education"}
TITLE_ONLY_WHEN_OMITTED = {"experience"}


@dataclass(frozen=True)
class FactLocation:
    """How a cited fact relates to the master being rendered."""

    position: int | None  # candidate position in this master, if it came from it
    entry: str | None


@dataclass
class TemplateResult:
    docx: bytes
    header_lines: list[str]
    notes: list[str] = field(default_factory=list)


def _bold_terms(paragraph: Paragraph) -> list[str]:
    terms = []
    full = paragraph.text.strip()
    for run in paragraph.runs:
        text = run.text.strip(" ,;:.")
        if run.bold and text and len(text) <= 40 and text != full:
            terms.append(text)
    return terms


def segments_for(text: str, bold_terms: list[str]) -> list[TextSegment]:
    """Split text into plain/bold segments, bolding whole-word occurrences of terms."""
    spans: list[tuple[int, int]] = []
    for term in sorted(set(bold_terms), key=len, reverse=True):
        for match in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text):
            if not any(match.start() < end and start < match.end() for start, end in spans):
                spans.append((match.start(), match.end()))
    spans.sort()
    segments, cursor = [], 0
    for start, end in spans:
        if start > cursor:
            segments.append(TextSegment(text[cursor:start]))
        segments.append(TextSegment(text[start:end], bold=True))
        cursor = end
    if cursor < len(text):
        segments.append(TextSegment(text[cursor:]))
    return segments or [TextSegment(text)]


def _project_terms(entry: str | None) -> list[str]:
    """'Access Review Tracker | Python, pandas, SQLite' -> ['Python', 'pandas', 'SQLite']."""
    if not entry or "|" not in entry:
        return []
    return [t.strip() for t in entry.split("|", 1)[1].split(",") if 0 < len(t.strip()) <= 40]


def _remove(paragraph: Paragraph) -> None:
    p = paragraph._p
    ppr = p.pPr
    if ppr is not None and ppr.find(qn("w:sectPr")) is not None:
        raise UnsupportedLayoutError("cannot remove a paragraph that ends a page section")
    p.getparent().remove(p)


def _set_text(paragraph: Paragraph, text: str, bold_terms: list[str]) -> None:
    replace_paragraph_text(paragraph, paragraph.text, segments_for(text, bold_terms))


def _clone_after(template: Paragraph, anchor: Paragraph) -> Paragraph:
    new_p = copy.deepcopy(template._p)
    anchor._p.addnext(new_p)
    return Paragraph(new_p, anchor._parent)


def render_template(
    master: bytes,
    content: DraftContent,
    facts: dict[uuid.UUID, FactLocation],
    prefs: FormatPreferences,
) -> TemplateResult:
    extraction = extract_docx(master)
    document = Document(io.BytesIO(master))
    paragraphs = body_paragraphs(document)
    by_position: dict[int, Candidate] = {c.position: c for c in extraction.candidates}

    # Group the master's bullets under their entry line.
    groups: dict[int, list[Candidate]] = {}
    for candidate in extraction.candidates:
        if candidate.entry_paragraph_index is not None:
            groups.setdefault(candidate.entry_paragraph_index, []).append(candidate)
    entry_text = {index: cands[0].entry for index, cands in groups.items()}
    entry_section = {index: cands[0].section for index, cands in groups.items()}

    # Match each draft entry to a master entry line.
    matched: dict[int, list] = {}
    for draft_entry in content.entries:
        index = None
        for bullet in draft_entry.bullets:
            for evidence_id in bullet.evidence_ids:
                location = facts.get(evidence_id)
                if location and location.position is not None and location.position in by_position:
                    index = by_position[location.position].entry_paragraph_index
                    break
            if index is not None:
                break
        if index is None:
            index = next((i for i, t in entry_text.items() if t == draft_entry.entry), None)
        if index is None:
            raise UnsupportedLayoutError("a drafted entry does not exist in this master")
        matched[index] = draft_entry.bullets

    bullet_template = next(
        (p for p in paragraphs if p.text.strip() and is_bullet(document, p)), None
    )
    to_remove: list[Paragraph] = []
    for index, candidates in groups.items():
        locked = (entry_text[index] or "") in prefs.locked_entries
        bullet_paras = [
            paragraphs[c.paragraph_index]
            for c in candidates
            if c.paragraph_index is not None and c.paragraph_index != index
        ]
        source_bold = [t for para in bullet_paras for t in _bold_terms(para)]
        if prefs.bold_project_technologies and entry_section[index] == "projects":
            source_bold += _project_terms(entry_text[index])
        if locked:
            continue
        if index in matched:
            # Caps only: never pad an entry with text to reach a bullet count.
            cap = (
                prefs.project_bullets
                if entry_section[index] == "projects"
                else prefs.max_bullets_per_entry
            )
            new_bullets = [
                b for b in matched[index] if not is_entry_line(b.text, entry_text[index])
            ][:cap]
            anchor = bullet_paras[-1] if bullet_paras else paragraphs[index]
            for i, bullet in enumerate(new_bullets):
                if i < len(bullet_paras):
                    _set_text(bullet_paras[i], bullet.text, source_bold)
                    anchor = bullet_paras[i]
                else:
                    template = bullet_paras[-1] if bullet_paras else bullet_template
                    if template is None:
                        raise UnsupportedLayoutError("no bullet paragraph to copy formatting from")
                    anchor = _clone_after(template, anchor)
                    _set_text(anchor, bullet.text, source_bold)
            to_remove.extend(bullet_paras[len(new_bullets) :])
        elif entry_section[index] in KEEP_WHEN_OMITTED:
            continue
        elif entry_section[index] in TITLE_ONLY_WHEN_OMITTED:
            to_remove.extend(bullet_paras)
        else:
            to_remove.extend(bullet_paras + [paragraphs[index]])

    # Skills and other list lines: rewrite the ones the draft cites; keep the rest.
    for skill in content.skills:
        location = next(
            (
                facts.get(e)
                for e in skill.evidence_ids
                if facts.get(e) and facts[e].position is not None
            ),
            None,
        )
        candidate = (
            by_position.get(location.position)
            if location and location.position is not None
            else None
        )
        if candidate is not None and candidate.paragraph_index is not None:
            para = paragraphs[candidate.paragraph_index]
            if para.text.strip() != skill.text:
                _set_text(para, skill.text, _bold_terms(para))

    for paragraph in to_remove:
        _remove(paragraph)
    _drop_empty_headings(document)

    buffer = io.BytesIO()
    document.save(buffer)
    return TemplateResult(docx=buffer.getvalue(), header_lines=extraction.header_lines)


def _drop_empty_headings(document) -> None:
    """Remove a section heading when nothing remains under it."""
    paragraphs = [p for p in body_paragraphs(document) if p.text.strip()]
    for i, paragraph in enumerate(paragraphs):
        if is_bullet(document, paragraph) or classify_heading(paragraph.text.strip()) is None:
            continue
        following = paragraphs[i + 1] if i + 1 < len(paragraphs) else None
        if following is None or (
            not is_bullet(document, following) and classify_heading(following.text.strip())
        ):
            _remove(paragraph)
