"""Choose which confirmed facts the drafting model reads (milestone 7, ADR 0010).

Pure functions: rankings come in, a selection with reasons comes out, so the
policy is testable without a database or a model.

Hybrid selection keeps, in this order of priority:
1. essential context: education and certification facts, always;
2. the top-k fused facts for every requirement;
3. exact-match fallback: if a requirement's posting term appears in some fact
   but no selected fact contains it, those facts are added (a retrieval miss
   must never hide a fact the deterministic matcher can see);
4. entry coverage: each job or internship keeps its best-ranked fact, so an
   employer never silently disappears from the context.
If the selection is still larger than the character budget, the lowest-ranked
facts that are not essential are dropped first.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.retrieval.search import Ranking, fuse
from app.tailoring.models import Requirement

STRATEGY_VERSION = "retrieval/v1"
ESSENTIAL_SECTION = re.compile(r"educat|degree|academic background|certif|licen", re.I)
COVERED_TYPES = {"professional", "internship"}
# Matches the per-fact cost build_context uses for its character budget.
FACT_OVERHEAD = 40


@dataclass(frozen=True)
class ProfileFact:
    evidence_id: uuid.UUID
    text: str
    section: str | None
    entry: str | None
    date_text: str | None
    experience_type: str

    @property
    def cost(self) -> int:
        return len(self.text) + len(self.entry or "") + FACT_OVERHEAD

    @property
    def entry_key(self) -> tuple | None:
        if self.entry is None:
            return None
        return (self.section, self.entry, self.date_text)


@dataclass(frozen=True)
class RequirementRankings:
    requirement: Requirement
    exact: Ranking
    fulltext: Ranking
    semantic: Ranking

    @property
    def fused(self) -> Ranking:
        return fuse([self.exact, self.fulltext, self.semantic])


@dataclass
class Selection:
    mode: str  # "full" | "hybrid"
    reason: str
    selected: list[uuid.UUID]  # profile order
    why: dict[uuid.UUID, str] = field(default_factory=dict)
    dropped_for_budget: int = 0

    def to_json(
        self, rankings: Sequence[RequirementRankings] = (), depth: int = 10
    ) -> dict[str, object]:
        """Provenance for storage: evidence IDs, ranks, and scores only (no fact text)."""

        def top(ranking: Ranking) -> list[list[object]]:
            return [[str(e), round(s, 4)] for e, s in ranking[:depth]]

        return {
            "selected": [str(e) for e in self.selected],
            "why": {str(e): reason for e, reason in self.why.items()},
            "dropped_for_budget": self.dropped_for_budget,
            "requirements": {
                r.requirement.id: {
                    "exact": top(r.exact),
                    "fulltext": top(r.fulltext),
                    "semantic": top(r.semantic),
                    "fused": top(r.fused),
                }
                for r in rankings
            },
        }


def full_selection(facts: Sequence[ProfileFact], reason: str) -> Selection:
    return Selection(
        "full", reason, [f.evidence_id for f in facts], {f.evidence_id: "full" for f in facts}
    )


def select_hybrid(
    facts: Sequence[ProfileFact],
    rankings: Sequence[RequirementRankings],
    *,
    top_k: int,
    budget_chars: int,
) -> Selection:
    by_id = {f.evidence_id: f for f in facts}
    why: dict[uuid.UUID, str] = {}
    priority: dict[uuid.UUID, float] = {}

    def add(evidence_id: uuid.UUID, reason: str, score: float) -> None:
        if evidence_id not in by_id:
            return  # a ranking may name a fact that is no longer confirmed
        why.setdefault(evidence_id, reason)
        priority[evidence_id] = max(priority.get(evidence_id, 0.0), score)

    for fact in facts:
        if fact.section and ESSENTIAL_SECTION.search(fact.section):
            add(fact.evidence_id, "essential", float("inf"))

    best: dict[uuid.UUID, float] = {}
    for item in rankings:
        fused = item.fused
        for evidence_id, score in fused:
            best[evidence_id] = max(best.get(evidence_id, 0.0), score)
        for evidence_id, score in fused[:top_k]:
            add(evidence_id, f"requirement:{item.requirement.id}", score)
        exact_ids = [e for e, _ in item.exact]
        if exact_ids and not any(e in why for e in exact_ids):
            for evidence_id in exact_ids[:top_k]:
                add(evidence_id, f"exact_fallback:{item.requirement.id}", best[evidence_id])

    entries: dict[tuple, list[ProfileFact]] = {}
    for fact in facts:
        if fact.experience_type in COVERED_TYPES and fact.entry_key is not None:
            entries.setdefault(fact.entry_key, []).append(fact)
    for group in entries.values():
        if not any(f.evidence_id in why for f in group):
            # The best-ranked fact of the entry, else its first fact.
            pick = max(group, key=lambda f: best.get(f.evidence_id, 0.0))
            add(pick.evidence_id, "entry_coverage", best.get(pick.evidence_id, 0.0))

    chosen = set(why)
    dropped = 0
    used = sum(by_id[e].cost for e in chosen)
    for evidence_id in sorted(chosen, key=lambda e: priority[e]):
        if used <= budget_chars:
            break
        if priority[evidence_id] == float("inf"):
            continue
        chosen.discard(evidence_id)
        used -= by_id[evidence_id].cost
        dropped += 1
    ordered = [f.evidence_id for f in facts if f.evidence_id in chosen]
    return Selection(
        "hybrid", "relevance", ordered, {e: why[e] for e in ordered}, dropped_for_budget=dropped
    )
