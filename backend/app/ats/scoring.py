"""The ATS match estimate: our documented rubric, computed in Python (ADR 0006).

This is an estimate from our own rubric, not any employer's score and not a
prediction of an interview. Per requirement (after merging duplicates):

- direct: every term appears affirmatively -> credit 1. Some terms -> the
  share found, never below a related match (1/2).
- related: no term, but a validated draft link to the requirement with shared
  wording, or strong shared wording (>= 2 stems and >= half of the
  requirement's) -> 1/2. Direct matches found only in academic or personal
  work for a requirement asking for professional experience are capped here.
- missing (terms exist, none found) and uncertain (nothing to check
  deterministically, only negated mentions, or a draft link without shared
  wording) -> 0, but listed separately for review.

Category score = mean credit; overall = weighted mean over the categories the
posting has (required 50, responsibility 25, preferred 15, terminology 10),
renormalized when a category is absent. Repeating a term never adds credit.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ats.matching import clean, content_stems, find_term, only_negated, snippet
from app.ats.passages import NON_WORK_TYPES, Passage
from app.tailoring.models import AnalysisContent, Category, Requirement

RUBRIC_VERSION = "ats-rubric/v1"
WEIGHTS: dict[Category, Fraction] = {
    "required": Fraction(50, 100),
    "responsibility": Fraction(25, 100),
    "preferred": Fraction(15, 100),
    "terminology": Fraction(10, 100),
}
# Duplicates keep the most important category.
CATEGORY_ORDER: tuple[Category, ...] = ("required", "responsibility", "preferred", "terminology")
RELATED = Fraction(1, 2)
MIN_SHARED_STEMS = 2
REPEAT_NOTICE_AT = 4
MAX_MATCHES = 5
_WORK_CONTEXT = re.compile(
    r"\b\d+\+?\s*(?:years?|yrs)\b|\bprofessional\b|\bindustry\b|\bwork experience\b", re.I
)

Status = Literal["direct", "related", "missing", "uncertain"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Match(_Model):
    kind: Literal["direct", "related", "negated"]
    basis: Literal["exact_term", "draft_link", "shared_wording"]
    where: str = Field(max_length=120)
    snippet: str = Field(max_length=200)
    term: str | None = None


class RequirementResult(_Model):
    id: str
    merged_ids: list[str]
    category: Category
    text: str
    mandatory: bool
    scored: bool
    status: Status
    credit: float = Field(ge=0, le=1)
    matched_terms: list[str]
    missing_terms: list[str]
    excluded_terms: list[str]
    notes: list[str]
    matches: list[Match]


class CategoryScore(_Model):
    category: Category
    requirements: int
    weight: float  # share of the overall estimate after renormalizing
    score: float  # 0-100


class TermCount(_Model):
    term: str
    mentions: int


class AtsResult(_Model):
    rubric_version: str
    analysis_fingerprint: str
    # None when nothing could be scored; never shown as 0.
    score: int | None = Field(default=None, ge=0, le=100)
    categories: list[CategoryScore]
    requirements: list[RequirementResult]
    mandatory_gaps: list[str]
    mandatory_partial: list[str]
    counts: dict[Status, int]
    repeated_terms: list[TermCount]


class Comparison(_Model):
    compatible: bool
    reason: str | None = None
    baseline: int | None = None
    tailored: int | None = None
    delta: int | None = None
    improved: list[str] = Field(default_factory=list)
    declined: list[str] = Field(default_factory=list)


def analysis_fingerprint(analysis: AnalysisContent) -> str:
    """Identifies the scored requirements, so only like-for-like reports are compared."""
    canonical = [
        [r.id, r.category, r.text, sorted(t.lower() for t in r.terms), r.mandatory]
        for r in analysis.requirements
    ]
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


def _dedupe(requirements: Sequence[Requirement]) -> list[tuple[Requirement, list[str]]]:
    """Merge requirements with the same terms (or same text when term-less)."""
    groups: dict[object, list[Requirement]] = {}
    for requirement in requirements:
        terms = frozenset(clean(t).strip().lower() for t in requirement.terms if t.strip())
        key = terms or " ".join(re.findall(r"\w+", requirement.text.lower()))
        groups.setdefault(key, []).append(requirement)
    merged = []
    for group in groups.values():
        keep = min(group, key=lambda r: CATEGORY_ORDER.index(r.category))
        if any(r.mandatory for r in group) and not keep.mandatory:
            keep = keep.model_copy(update={"mandatory": True})
        merged.append((keep, [r.id for r in group if r.id != keep.id]))
    return merged


@dataclass
class _Assessment:
    status: Status
    credit: Fraction
    scored: bool = True
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)


def _assess(
    requirement: Requirement,
    ids: set[str],
    passages: Sequence[Passage],
    job_text: str | None,
) -> _Assessment:
    terms = list(dict.fromkeys(clean(t).strip() for t in requirement.terms if t.strip()))
    excluded = [t for t in terms if job_text and only_negated(job_text, t)]
    active = [t for t in terms if t not in excluded]

    direct: dict[str, list[tuple[Passage, int, int]]] = {}
    negated: list[Match] = []
    for term in active:
        for passage in passages:
            for hit in find_term(passage.text, term):
                if hit.negated:
                    negated.append(
                        Match(
                            kind="negated",
                            basis="exact_term",
                            where=passage.where[:120],
                            snippet=snippet(passage.text, hit.start, hit.end),
                            term=term,
                        )
                    )
                else:
                    direct.setdefault(term, []).append((passage, hit.start, hit.end))

    stems = content_stems(" ".join([requirement.text, *terms]))
    related: list[Match] = []
    unsupported_links: list[str] = []
    for passage in passages:
        shared = stems & content_stems(passage.text)
        linked = bool(ids & passage.requirement_ids)
        if linked and shared:
            basis: Literal["draft_link", "shared_wording"] = "draft_link"
        elif len(shared) >= MIN_SHARED_STEMS and 2 * len(shared) >= len(stems):
            basis = "shared_wording"
        else:
            if linked:
                unsupported_links.append(passage.where)
            continue
        related.append(
            Match(
                kind="related",
                basis=basis,
                where=passage.where[:120],
                snippet=snippet(passage.text, 0, 0, width=180),
            )
        )

    found = [t for t in active if t in direct]
    result = _Assessment(
        status="missing",
        credit=Fraction(0),
        matched=found,
        missing=[t for t in active if t not in direct],
        excluded=excluded,
    )
    if excluded:
        result.notes.append(
            "The posting says it does not need: " + ", ".join(excluded) + " (not scored)."
        )
    if terms and not active:
        result.status, result.scored = "uncertain", False
        return result

    direct_matches = [
        Match(
            kind="direct",
            basis="exact_term",
            where=p.where[:120],
            snippet=snippet(p.text, start, end),
            term=term,
        )
        for term in found
        for p, start, end in direct[term][:2]
    ]
    if found:
        result.status = "direct"
        result.credit = (
            Fraction(1)
            if len(found) == len(active)
            else max(Fraction(len(found), len(active)), RELATED)
        )
        if len(found) < len(active):
            result.notes.append(f"Found {len(found)} of {len(active)} terms.")
        types = {p.experience_type for term in found for p, _, _ in direct[term]}
        if _WORK_CONTEXT.search(requirement.text) and types <= NON_WORK_TYPES:
            result.status, result.credit = "related", min(result.credit, RELATED)
            result.notes.append(
                "Asks for professional experience; found only in academic or personal work."
            )
        result.matches = (direct_matches + related)[:MAX_MATCHES]
    elif related:
        result.status, result.credit = "related", RELATED
        result.matches = related[:MAX_MATCHES]
    else:
        result.status = "missing" if active and not negated else "uncertain"
        if negated:
            result.notes.append("Mentioned only in a negative phrase.")
        if not active:
            result.notes.append("No specific terms to check; review this one yourself.")
        result.matches = negated[:MAX_MATCHES]
    if unsupported_links and result.status in {"missing", "uncertain"}:
        result.status = "uncertain"
        result.notes.append(
            "Linked to "
            + ", ".join(unsupported_links[:2])
            + " when drafting, but they share no wording; review it."
        )
    return result


def _round(value: Fraction) -> int:
    return math.floor(value + Fraction(1, 2))


def score(
    analysis: AnalysisContent, passages: Sequence[Passage], *, job_text: str | None = None
) -> AtsResult:
    """Score passages against the analysis. ``job_text`` enables posting-side negation."""
    job = clean(job_text) if job_text else None
    results: list[RequirementResult] = []
    credits: dict[Category, list[Fraction]] = {c: [] for c in CATEGORY_ORDER}
    for requirement, merged in _dedupe(analysis.requirements):
        assessed = _assess(requirement, {requirement.id, *merged}, passages, job)
        if assessed.scored:
            credits[requirement.category].append(assessed.credit)
        results.append(
            RequirementResult(
                id=requirement.id,
                merged_ids=merged,
                category=requirement.category,
                text=requirement.text,
                mandatory=requirement.mandatory,
                scored=assessed.scored,
                status=assessed.status,
                credit=round(float(assessed.credit), 4),
                matched_terms=assessed.matched,
                missing_terms=assessed.missing,
                excluded_terms=assessed.excluded,
                notes=assessed.notes[:5],
                matches=assessed.matches,
            )
        )

    present = [c for c in CATEGORY_ORDER if credits[c]]
    total_weight = sum((WEIGHTS[c] for c in present), Fraction(0))
    overall = Fraction(0)
    categories = []
    for category in present:
        mean = sum(credits[category], Fraction(0)) / len(credits[category])
        share = WEIGHTS[category] / total_weight
        overall += share * mean
        categories.append(
            CategoryScore(
                category=category,
                requirements=len(credits[category]),
                weight=round(float(share), 4),
                score=round(float(mean * 100), 1),
            )
        )

    mentions: dict[str, TermCount] = {}
    for result in results:
        for term in result.matched_terms:
            count = sum(1 for p in passages for hit in find_term(p.text, term) if not hit.negated)
            if count >= REPEAT_NOTICE_AT:
                mentions.setdefault(term.lower(), TermCount(term=term, mentions=count))

    scored = [r for r in results if r.scored]
    return AtsResult(
        rubric_version=RUBRIC_VERSION,
        analysis_fingerprint=analysis_fingerprint(analysis),
        score=_round(overall * 100) if present else None,
        categories=categories,
        requirements=results,
        mandatory_gaps=[
            r.id for r in scored if r.mandatory and r.status in {"missing", "uncertain"}
        ],
        mandatory_partial=[
            r.id
            for r in scored
            if r.mandatory and r.status in {"direct", "related"} and r.credit < 1
        ],
        counts={
            s: sum(1 for r in scored if r.status == s)
            for s in ("direct", "related", "missing", "uncertain")
        },
        repeated_terms=list(mentions.values()),
    )


def compare(baseline: AtsResult, tailored: AtsResult) -> Comparison:
    """Compare only reports made with the same rubric and the same requirements."""
    if baseline.rubric_version != tailored.rubric_version:
        return Comparison(compatible=False, reason="Scored with different rubric versions.")
    if baseline.analysis_fingerprint != tailored.analysis_fingerprint:
        return Comparison(compatible=False, reason="Scored against different job analyses.")
    before = {r.id: r.credit for r in baseline.requirements if r.scored}
    after = {r.id: r.credit for r in tailored.requirements if r.scored}
    delta = None
    if baseline.score is not None and tailored.score is not None:
        delta = tailored.score - baseline.score
    return Comparison(
        compatible=True,
        baseline=baseline.score,
        tailored=tailored.score,
        delta=delta,
        improved=[i for i in after if after[i] > before.get(i, 0)],
        declined=[i for i in after if after[i] < before.get(i, 0)],
    )
