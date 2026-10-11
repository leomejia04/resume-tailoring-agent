"""Turn a draft, a master's text, or a rendered PDF's text into scoreable passages.

Draft passages carry the experience type of their entry and the requirement
links that survived validation. Raw text (masters, unattributed PDF text) has
neither, so it can only earn direct term matches or shared-wording matches.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.ats.matching import clean
from app.documents.pdf_inspect import normalize
from app.render.format import is_entry_line
from app.tailoring.models import DraftContent

WORK_TYPES = frozenset({"professional", "internship"})
NON_WORK_TYPES = frozenset({"academic", "personal"})
MAX_WINDOW_WORDS = 40


@dataclass(frozen=True)
class Passage:
    where: str
    text: str
    requirement_ids: frozenset[str] = frozenset()
    # None when unknown: raw master or PDF text, skills lines, headline, summary.
    experience_type: str | None = None


def _label(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def draft_passages(content: DraftContent) -> list[Passage]:
    """What the draft will show, in order. A hidden headline is not scored."""
    passages: list[Passage] = []
    if content.format.show_headline and content.headline:
        passages.append(Passage("Headline", clean(content.headline)))
    if content.summary:
        passages.append(Passage("Summary", clean(content.summary)))
    for entry in content.entries:
        label = _label(entry.entry or (entry.section or "entry").title())
        kind = entry.experience_type
        line_links = frozenset(
            r
            for b in entry.bullets
            if is_entry_line(b.text, entry.entry)
            for r in b.requirement_ids
        )
        if entry.entry:
            passages.append(Passage(label, clean(entry.entry), line_links, kind))
        number = 0
        for bullet in entry.bullets:
            if is_entry_line(bullet.text, entry.entry):
                continue  # rendered as the entry line itself
            number += 1
            passages.append(
                Passage(
                    f"{label} · bullet {number}",
                    clean(bullet.text),
                    frozenset(bullet.requirement_ids),
                    kind,
                )
            )
    passages += [Passage("Skills", clean(skill.text)) for skill in content.skills]
    return passages


def text_passages(text: str, label: str) -> list[Passage]:
    """One passage per line; a line starting in lowercase continues the previous one."""
    blocks: list[tuple[int, str]] = []
    for number, raw in enumerate(clean(text).splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if blocks and line[0].islower():
            blocks[-1] = (blocks[-1][0], f"{blocks[-1][1]} {line}")
        else:
            blocks.append((number, line))
    return [Passage(f"{label}, line {n}", t) for n, t in blocks]


def _windows(text: str) -> list[str]:
    words = text.split()
    return [
        " ".join(words[i : i + MAX_WINDOW_WORDS]) for i in range(0, len(words), MAX_WINDOW_WORDS)
    ]


def pdf_passages(content: DraftContent, pdf_text: str) -> list[Passage]:
    """Score what the PDF really contains.

    Draft passages found in the PDF text keep their context and links; anything
    dropped to fit the page or not rendered by the layout is simply absent.
    The rest of the PDF text (contact lines, headings, reformatted lines) is
    scored as unattributed text. Longer passages are located first so a short
    one cannot claim part of a bullet.
    """
    remaining = normalize(clean(pdf_text))
    found: list[Passage] = []
    for passage in sorted(draft_passages(content), key=lambda p: -len(p.text)):
        needle = normalize(passage.text)
        position = remaining.find(needle) if needle else -1
        if position == -1:
            continue
        found.append(replace(passage, where=f"{passage.where} (PDF)"))
        remaining = remaining[:position] + "\n" + remaining[position + len(needle) :]
    residual = [
        Passage("PDF text", window) for chunk in remaining.split("\n") for window in _windows(chunk)
    ]
    return found + residual
