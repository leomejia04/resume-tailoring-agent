"""Normalize model job analyses and find requirements the user's facts do not cover."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ai.schemas import JobAnalysisOut
from app.tailoring.models import AnalysisContent, Requirement
from app.tailoring.validate import contains_term

MAX_REQUIREMENTS = 40
MAX_QUESTIONS = 8
# Words too generic to ask about or to treat as evidence of a skill.
GENERIC_TERMS = {
    "experience", "skills", "skill", "team", "teams", "communication", "ability", "work",
    "years", "degree", "bachelor", "strong", "excellent", "knowledge", "tools", "data",
}  # fmt: skip


def _key(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def normalize_analysis(raw: JobAnalysisOut, job_text: str) -> AnalysisContent:
    """Deduplicate, bound, and renumber requirements; keep only terms found in the posting."""
    requirements: list[Requirement] = []
    seen: set[str] = set()
    for item in raw.requirements:
        text = re.sub(r"\s+", " ", item.text).strip()[:300]
        key = _key(text)
        if not text or key in seen:
            continue
        seen.add(key)
        terms: list[str] = []
        for term in item.terms:
            term = term.strip()[:60]
            # Terms must literally appear in the posting: no invented keywords.
            if (
                term
                and term.lower() not in GENERIC_TERMS
                and term.lower() not in {t.lower() for t in terms}
                and contains_term(job_text, term)
            ):
                terms.append(term)
        requirements.append(
            Requirement(
                id=f"R{len(requirements) + 1}",
                category=item.category,
                text=text,
                terms=terms[:5],
                mandatory=item.mandatory and item.category == "required",
            )
        )
        if len(requirements) >= MAX_REQUIREMENTS:
            break
    title = re.sub(r"\s+", " ", raw.role_title).strip()[:120] or "Target role"
    seniority = (raw.seniority or "").strip()[:60] or None
    return AnalysisContent(role_title=title, seniority=seniority, requirements=requirements)


@dataclass(frozen=True)
class Gap:
    requirement: Requirement
    term: str


def find_gaps(analysis: AnalysisContent, verified_text: str) -> list[Gap]:
    """Required/preferred requirements with terms that no confirmed fact mentions."""
    gaps: list[Gap] = []
    asked: set[str] = set()
    ordered = sorted(
        (r for r in analysis.requirements if r.category in {"required", "preferred"}),
        key=lambda r: (not r.mandatory, r.category != "required"),
    )
    for requirement in ordered:
        if not requirement.terms:
            continue
        if any(contains_term(verified_text, t) for t in requirement.terms):
            continue
        term = next((t for t in requirement.terms if t.lower() not in asked), None)
        if term is None:
            continue
        asked.add(term.lower())
        gaps.append(Gap(requirement, term))
        if len(gaps) >= MAX_QUESTIONS:
            break
    return gaps


def question_for(gap: Gap) -> str:
    return (
        f'The posting asks for {gap.term} ("{gap.requirement.text[:160]}"). '
        f"Have you used {gap.term}? If so, where, and what did you do with it?"
    )
