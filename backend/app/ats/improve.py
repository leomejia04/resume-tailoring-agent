"""Which gaps a draft could close with the user's own confirmed facts, and whether
a candidate revision really improves the estimate (milestone 6d, ADR 0008).

Pure functions over the ATS rubric: no database, no model. The model may only
propose; these rules decide.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.ats.matching import clean
from app.ats.passages import Passage, draft_passages
from app.ats.scoring import CATEGORY_ORDER, AtsResult, score
from app.tailoring.context import Fact
from app.tailoring.models import AnalysisContent, DraftContent

# Requirements one attempt works on; keeps the request and the patch small.
MAX_TARGETS = 8

Verdict = Literal["improved", "no_improvement", "new_repetition"]


@dataclass(frozen=True)
class Target:
    requirement_id: str
    text: str
    terms: list[str]
    mandatory: bool
    # Aliases (E1, E2...) of confirmed facts that earn more credit than the draft does.
    fact_aliases: list[str]


@dataclass(frozen=True)
class Plan:
    before: AtsResult
    targets: list[Target]
    # Scored requirements below full credit, supported or not.
    open_gaps: int


def fact_passages(facts: Iterable[Fact]) -> list[Passage]:
    """Each confirmed fact as its own passage, labeled with its alias."""
    return [Passage(f.alias, clean(f.text), experience_type=f.experience_type) for f in facts]


def plan(
    analysis: AnalysisContent,
    content: DraftContent,
    facts: Iterable[Fact],
    job_text: str | None,
) -> Plan:
    """Targets: requirements the confirmed facts would earn more credit for than the draft.

    Both sides are scored with the same rubric, so a fact counts only where it
    would count on the page (whole terms, no negation, the professional-work
    cap). Requirements no confirmed fact supports are never targets.
    """
    before = score(analysis, draft_passages(content), job_text=job_text)
    supported = {
        r.id: r for r in score(analysis, fact_passages(facts), job_text=job_text).requirements
    }
    source = {r.id: r for r in analysis.requirements}
    targets: list[Target] = []
    open_gaps = 0
    for result in before.requirements:
        if not result.scored or result.credit >= 1:
            continue
        open_gaps += 1
        better = supported.get(result.id)
        if better is None or not better.scored or better.credit <= result.credit:
            continue
        aliases = list(
            dict.fromkeys(m.where for m in better.matches if m.kind in ("direct", "related"))
        )
        if not aliases:
            continue
        requirement = source[result.id]
        targets.append(
            Target(
                requirement_id=result.id,
                text=requirement.text,
                terms=list(requirement.terms),
                mandatory=result.mandatory,
                fact_aliases=aliases,
            )
        )
    targets.sort(
        key=lambda t: (not t.mandatory, CATEGORY_ORDER.index(source[t.requirement_id].category))
    )
    return Plan(before=before, targets=targets[:MAX_TARGETS], open_gaps=open_gaps)


def judge(
    analysis: AnalysisContent,
    before: AtsResult,
    candidate: DraftContent,
    job_text: str | None,
) -> tuple[Verdict, AtsResult, list[str]]:
    """Accept only a strict improvement: some requirement gains credit, none loses
    any, and no term is repeated more than before. Returns the verdict, the
    candidate's result, and the requirement IDs that improved."""
    after = score(analysis, draft_passages(candidate), job_text=job_text)
    old = {r.id: r.credit for r in before.requirements if r.scored}
    new = {r.id: r.credit for r in after.requirements if r.scored}
    declined = [i for i, credit in old.items() if new.get(i, 0) < credit]
    improved = [i for i, credit in new.items() if credit > old.get(i, 0)]
    if declined or not improved or (after.score or 0) < (before.score or 0):
        return "no_improvement", after, improved
    repeated = {t.term.lower(): t.mentions for t in before.repeated_terms}
    if any(t.mentions > repeated.get(t.term.lower(), 0) for t in after.repeated_terms):
        return "new_repetition", after, improved
    return "improved", after, improved
