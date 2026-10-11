"""Conservative text matching for the ATS match estimate.

A term counts only as a whole word or phrase (Java never matches JavaScript),
only in an affirmative clause ("no SQL experience" does not count), and short
or ordinary-word terms (R, C, Go, Excel) must keep the posting's
capitalization and must not sit inside compounds such as R&D, C++, or
go-to-market. Spacing and hyphens inside a phrase are flexible (Power BI,
Power-BI, PowerBI), and a simple plural is accepted for longer words.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

# Ordinary English words that are also tool or language names.
COMMON_WORD_TERMS = {
    "access", "ada", "chef", "dart", "elm", "excel", "go", "hive", "julia", "outlook",
    "pig", "puppet", "ruby", "rust", "sheets", "slack", "spark", "swift", "teams", "word",
}  # fmt: skip

NEGATION_CUES = {"no", "not", "without", "never", "lack", "lacks", "lacking", "unfamiliar", "non"}
# Words that may sit between a negation cue and the term ("no prior experience with SQL").
NEGATION_FILLER = {
    "prior", "previous", "professional", "formal", "direct", "hands-on", "experience",
    "experienced", "knowledge", "exposure", "familiarity", "with", "of", "in", "on", "using",
    "use", "used", "the", "a", "an", "any", "working", "worked", "familiar",
}  # fmt: skip
MAX_FILLER = 3
_AFTER_NEGATION = re.compile(
    r"^\W*(?:(?:experience|knowledge|skills?|background)\s+)?(?:is|are)?\s*"
    r"(?:not|n't)\s+(?:required|necessary|needed|expected)\b",
    re.I,
)
# Clause boundaries for negation scope. Commas are not boundaries: "Python, not R".
_CLAUSE_BREAK = re.compile(r"[.;!?\n|•●▪]")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")

STOPWORDS = {
    "a", "about", "across", "after", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "been", "being", "both", "but", "by", "can", "do", "each", "etc", "for", "from",
    "has", "have", "how", "in", "including", "into", "is", "it", "its", "may", "more", "must",
    "of", "on", "or", "other", "our", "over", "per", "plus", "such", "than", "that", "the",
    "their", "them", "these", "this", "those", "through", "to", "under", "up", "use", "used",
    "using", "via", "was", "we", "were", "what", "when", "where", "which", "while", "who",
    "will", "with", "within", "would", "you", "your",
    # Job-posting filler that says nothing about a specific skill.
    "ability", "able", "abilities", "candidate", "demonstrated", "desired", "excellent",
    "experience", "experienced", "familiarity", "good", "great", "ideal", "knowledge",
    "minimum", "preferred", "proficiency", "proficient", "proven", "related",
    "relevant", "required", "requirement", "requirements", "responsible", "role", "skill",
    "skills", "solid", "strong", "understanding", "work", "working", "year", "years",
}  # fmt: skip


@dataclass(frozen=True)
class TermHit:
    start: int
    end: int
    negated: bool


def clean(text: str) -> str:
    """Unicode-normalize (ligatures, full-width forms) and unify dashes and quotes."""
    text = unicodedata.normalize("NFKC", text)
    return text.replace("’", "'").replace("–", "-").replace("—", "-")


def exact_case(term: str) -> bool:
    """Short or ordinary-word terms must keep the posting's capitalization."""
    return len(term) <= 2 or term.lower() in COMMON_WORD_TERMS


def _last_token_pattern(token: str) -> str:
    lower = token.lower()
    if token.isupper() or not re.fullmatch(r"[a-z]{4,}", lower):
        return re.escape(token)
    base = token[:-1] if lower.endswith("s") and not lower.endswith("ss") else token
    return re.escape(base) + "(?:s|es)?"


@lru_cache(maxsize=2048)
def term_regex(term: str) -> re.Pattern[str]:
    tokens = term.split()
    if not tokens:
        raise ValueError("empty term")
    exact = exact_case(term)
    last = re.escape(tokens[-1]) if exact else _last_token_pattern(tokens[-1])
    body = r"[\s-]*".join([re.escape(t) for t in tokens[:-1]] + [last])
    if len(term) <= 2:
        # Not part of R&D, C++, C#, C-suite, Go-to-market, or R/3.
        before = r"(?<![A-Za-z0-9+#&])(?<![A-Za-z0-9][-/.&])"
        after = r"(?![A-Za-z0-9+#&])(?![-/.&][A-Za-z0-9])"
        return re.compile(before + body + after)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", 0 if exact else re.I)


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = max((m.end() for m in _CLAUSE_BREAK.finditer(text, 0, start)), default=0)
    right_match = _CLAUSE_BREAK.search(text, end)
    return left, right_match.start() if right_match else len(text)


def _negated(text: str, start: int, end: int) -> bool:
    left, right = _clause_bounds(text, start, end)
    words = [w.lower().strip("-'") for w in _WORD.findall(text[left:start])]
    skipped = 0
    for word in reversed(words):
        if word in NEGATION_CUES or word.endswith("n't"):
            return True
        if word in NEGATION_FILLER and skipped < MAX_FILLER:
            skipped += 1
            continue
        break
    return bool(_AFTER_NEGATION.match(text[end:right]))


def find_term(text: str, term: str) -> list[TermHit]:
    term = clean(term).strip()
    if not term:
        return []
    return [
        TermHit(m.start(), m.end(), _negated(text, m.start(), m.end()))
        for m in term_regex(term).finditer(text)
    ]


def only_negated(text: str, term: str) -> bool:
    """True when the text mentions the term, but only in negative phrases."""
    hits = find_term(clean(text), term)
    return bool(hits) and all(h.negated for h in hits)


# Irregular past tenses common in resume bullets ("Built", "Led", "Wrote").
IRREGULAR = {
    "built": "build", "led": "lead", "wrote": "write", "written": "write", "ran": "run",
    "made": "make", "drove": "drive", "driven": "drive", "grew": "grow", "grown": "grow",
    "taught": "teach", "sold": "sell", "bought": "buy", "met": "meet", "began": "begin",
    "won": "win", "chose": "choose", "spoke": "speak", "kept": "keep", "held": "hold",
    "found": "find", "brought": "bring", "thought": "think", "gave": "give", "given": "give",
}  # fmt: skip


def stem(word: str) -> str:
    word = IRREGULAR.get(word, word)
    for suffix in ("ments", "ment", "ings", "ing", "ed", "es", "s"):
        if suffix == "s" and word.endswith("ss"):
            break
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) >= 5 else word


def content_stems(text: str) -> set[str]:
    """Meaningful word stems, for the shared-wording check behind "related" evidence."""
    words = re.findall(r"[a-z][a-z0-9+#]*", clean(text).lower())
    return {stem(w) for w in words if len(w) >= 3 and w not in STOPWORDS}


def snippet(text: str, start: int, end: int, width: int = 70) -> str:
    left, right = max(0, start - width), min(len(text), end + width)
    body = " ".join(text[left:right].split())
    return f"{'…' if left else ''}{body}{'…' if right < len(text) else ''}"[:200]
