"""Deterministic clean-up and heading detection for pasted job postings.

This is plain Python, not AI: it finds likely section headings so the user can
see what was recognized. Semantic job analysis comes later and will be saved
per job version.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​﻿]")
_BULLET_START = re.compile(r"^\s*(?:[-*•●▪]|\d+[.)])\s+")
_HEADING_DECORATION = re.compile(r"^[#\s]+|[\s:]+$")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'"})

# Checked in order: the first matching kind wins, so "preferred qualifications"
# is classified before the generic "qualifications".
_KINDS: list[tuple[str, re.Pattern[str]]] = [
    ("preferred", re.compile(r"\b(preferred|nice to have|nice-to-have|bonus|desired|plus)\b")),
    ("about_company", re.compile(r"\b(about us|about the company|who we are|our company)\b")),
    (
        "about_role",
        re.compile(r"\b(about the (role|job|position)|job summary|overview|position summary)\b"),
    ),
    ("benefits", re.compile(r"\b(benefits|perks|what we offer|compensation)\b")),
    (
        "responsibilities",
        re.compile(
            r"\b(responsibilities|what you'll do|duties|day to day|day-to-day|your impact)\b"
        ),
    ),
    (
        "required",
        re.compile(
            r"\b(requirements|qualifications|must have|must-have|what you'll need|"
            r"what we're looking for|who you are|skills|experience)\b"
        ),
    ),
]


@dataclass(frozen=True)
class Heading:
    kind: str
    text: str
    line: int


def normalize_job_text(text: str) -> str:
    """Unicode-normalize, drop control characters, and tidy whitespace."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text).replace("\t", " ")
    lines = [re.sub(r" {2,}", " ", line).rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _looks_like_heading(line: str) -> bool:
    if _BULLET_START.match(line):
        return False
    stripped = line.strip()
    candidate = _HEADING_DECORATION.sub("", stripped)
    words = candidate.split()
    if not words or len(candidate) > 60 or len(words) > 7:
        return False
    if stripped.endswith(":") or stripped.startswith("#"):
        return True
    if candidate.endswith((".", ",", ";")):
        return False
    # Without a colon, require Title Case or ALL CAPS so sentences are not headings.
    significant = [w for w in words if len(w) > 3 and w[0].isalpha()]
    return bool(significant) and all(w[0].isupper() for w in significant)


def find_headings(text: str) -> list[Heading]:
    headings = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not _looks_like_heading(line):
            continue
        label = _HEADING_DECORATION.sub("", line.strip())
        key = label.lower().translate(_APOSTROPHES)
        for kind, pattern in _KINDS:
            if pattern.search(key):
                headings.append(Heading(kind=kind, text=label[:120], line=number))
                break
    return headings


def word_count(text: str) -> int:
    return len(re.findall(r"\w+(?:['’-]\w+)*", text))
