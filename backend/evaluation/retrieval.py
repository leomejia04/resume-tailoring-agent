"""Retrieval evaluation on the labeled fictional set (milestone 7, ADR 0010).

Compares, per requirement, the rankings the app actually uses (exact terms,
Postgres full-text, pgvector semantic, and their Reciprocal Rank Fusion) and,
per posting, the context the drafting job would send (full profile vs the
hybrid selection policy). It runs the production code against the local
Supabase database as fictional evaluation users, under row-level security,
with the real pinned embedding model. Nothing is sent to any external AI
provider and nothing here costs money.

Two scenarios per posting:
- own: the posting against its role's profile (17-22 facts);
- combined: the posting against all four profiles in one experience bank
  (78 facts), a distractor-heavy stress test, not a realistic person.

Metrics (only over requirements that have labels in the scenario):
- recall@k: share of labeled direct+related facts in the top k;
- direct hit@k: at least one direct fact in the top k;
- precision@k: labeled facts in the top k divided by k;
- MRR: mean of 1 / rank of the first direct fact (0 if absent from the list);
- selection coverage: share of labeled (requirement, fact) pairs whose fact is
  in the context the drafting model would read, and facts/characters sent.
"""

from __future__ import annotations

import secrets
import statistics
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.gotrue import SupabaseAuthClient
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import EvidenceRecord, EvidenceVersion
from app.db.session import user_session
from app.retrieval import index
from app.retrieval.embedder import Embedder
from app.retrieval.search import (
    Candidate,
    Ranking,
    exact_ranking,
    fulltext_ranking,
    fuse,
    query_text,
    semantic_ranking,
)
from app.retrieval.selection import ProfileFact, RequirementRankings, select_hybrid
from app.tailoring.models import Category, Requirement

DATASET = Path(__file__).resolve().parent / "data" / "retrieval_v1.json"
METHODS = ("exact", "fulltext", "semantic", "hybrid")
KS = (1, 3, 5, 10)
COMBINED = "combined"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactIn(_Model):
    id: str = Field(pattern=r"^[a-z]\d{2}$")
    section: str | None
    entry: str | None
    date_text: str | None
    experience_type: Literal["professional", "internship", "academic", "personal", "other"]
    text: str = Field(min_length=1, max_length=2000)


class RequirementIn(_Model):
    id: str = Field(pattern=r"^R\d{1,3}$")
    category: Category
    mandatory: bool
    text: str = Field(min_length=1, max_length=300)
    terms: list[str] = Field(max_length=5)
    direct: list[str]
    related: list[str]

    def as_requirement(self) -> Requirement:
        return Requirement(
            id=self.id,
            category=self.category,
            text=self.text,
            terms=self.terms,
            mandatory=self.mandatory,
        )


class PostingIn(_Model):
    id: str
    role: str
    profile: str
    title: str
    company: str
    requirements: list[RequirementIn]


class Dataset(_Model):
    version: str
    about: str
    profiles: dict[str, list[FactIn]]
    postings: list[PostingIn]

    @model_validator(mode="after")
    def _labels_are_consistent(self) -> Dataset:
        ids = [f.id for facts in self.profiles.values() for f in facts]
        if len(ids) != len(set(ids)):
            raise ValueError("fact ids must be unique across profiles")
        known = set(ids)
        for posting in self.postings:
            if posting.profile not in self.profiles:
                raise ValueError(f"{posting.id}: unknown profile {posting.profile}")
            req_ids = [r.id for r in posting.requirements]
            if len(req_ids) != len(set(req_ids)):
                raise ValueError(f"{posting.id}: duplicate requirement ids")
            for req in posting.requirements:
                labels = req.direct + req.related
                if len(labels) != len(set(labels)):
                    raise ValueError(f"{posting.id} {req.id}: a fact is labeled twice")
                unknown = set(labels) - known
                if unknown:
                    raise ValueError(f"{posting.id} {req.id}: unknown facts {sorted(unknown)}")
        return self

    def facts_for(self, profile: str) -> list[FactIn]:
        if profile == COMBINED:
            return [f for facts in self.profiles.values() for f in facts]
        return self.profiles[profile]


def load_dataset(path: Path = DATASET) -> Dataset:
    return Dataset.model_validate_json(path.read_text(encoding="utf-8"))


# --- Metrics (pure) ---------------------------------------------------------------


def recall_at(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant)


def precision_at(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / k


def hit_at(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return 1.0 if set(ranked[:k]) & relevant else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for rank, fact_id in enumerate(ranked, start=1):
        if fact_id in relevant:
            return 1.0 / rank
    return 0.0


def mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return round(statistics.fmean(values), 4) if values else None


# --- Running against the database -------------------------------------------------


@dataclass
class EvalUser:
    user: VerifiedUser
    fact_ids: dict[str, uuid.UUID]  # dataset id -> evidence id
    facts: list[ProfileFact]
    index_ms: int
    reindex_ms: int
    reindex_embedded: int


@dataclass
class ScenarioResult:
    posting: str
    role: str
    scenario: str
    rankings: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # req -> method
    timings_ms: dict[str, float] = field(default_factory=dict)
    selection: dict[str, object] = field(default_factory=dict)


def create_eval_user(settings: Settings) -> VerifiedUser:
    email = f"retrieval-eval-{secrets.token_hex(6)}@example.test"
    session = SupabaseAuthClient(settings).sign_up(email, secrets.token_urlsafe(18))
    if session is None:
        raise SystemExit("Local sign-up requires email confirmation; disable it for local runs.")
    return VerifiedUser(id=session.user_id, email=email, access_token=session.access_token)


def load_profile(
    settings: Settings, user: VerifiedUser, facts: Sequence[FactIn], embedder: Embedder
) -> EvalUser:
    mapping: dict[str, uuid.UUID] = {}
    refs: list[index.FactRef] = []
    with user_session(settings, user.id) as db:
        for position, fact in enumerate(facts):
            record = EvidenceRecord(
                id=uuid.uuid4(),
                owner_id=user.id,
                master_version_id=None,
                origin="confirmation",
                position=position,
                current_version=1,
            )
            db.add(record)
            db.flush()
            db.add(
                EvidenceVersion(
                    evidence_id=record.id,
                    owner_id=user.id,
                    version=1,
                    text=fact.text,
                    source_text=fact.text,
                    section=fact.section,
                    entry=fact.entry,
                    date_text=fact.date_text,
                    metrics=[],
                    experience_type=fact.experience_type,
                    suggested_type=fact.experience_type,
                    verification_status="verified",
                    change_source="confirmation",
                )
            )
            mapping[fact.id] = record.id
            refs.append(
                index.FactRef(
                    record.id, 1, fact.text, fact.section, fact.entry, fact.experience_type
                )
            )
    started = time.perf_counter()
    with user_session(settings, user.id) as db:
        index.sync_chunks(db, user.id, refs, embedder)
    index_ms = int((time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    with user_session(settings, user.id) as db:
        again = index.sync_chunks(db, user.id, refs, embedder)
    reindex_ms = int((time.perf_counter() - started) * 1000)
    profile = [
        ProfileFact(mapping[f.id], f.text, f.section, f.entry, f.date_text, f.experience_type)
        for f in facts
    ]
    return EvalUser(user, mapping, profile, index_ms, reindex_ms, again.embedded)


def run_scenario(
    settings: Settings,
    account: EvalUser,
    posting: PostingIn,
    scenario: str,
    embedder: Embedder,
    *,
    top_k: int,
    budget_chars: int,
) -> ScenarioResult:
    back = {v: k for k, v in account.fact_ids.items()}
    result = ScenarioResult(posting.id, posting.role, scenario)
    requirements = [r.as_requirement() for r in posting.requirements]
    candidates = [Candidate(f.evidence_id, f.text) for f in account.facts]

    started = time.perf_counter()
    exact = [exact_ranking(r, candidates) for r in requirements]
    result.timings_ms["exact"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    vectors = embedder.embed([query_text(r) for r in requirements])
    result.timings_ms["query_embedding"] = (time.perf_counter() - started) * 1000

    fulltext: list[Ranking] = []
    semantic: list[Ranking] = []
    with user_session(settings, account.user.id) as db:
        started = time.perf_counter()
        for requirement in requirements:
            fulltext.append(fulltext_ranking(db, account.user.id, requirement))
        result.timings_ms["fulltext"] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        for vector in vectors:
            semantic.append(semantic_ranking(db, account.user.id, embedder, vector))
        result.timings_ms["semantic_sql"] = (time.perf_counter() - started) * 1000

    bundles = [
        RequirementRankings(req, e, f, s)
        for req, e, f, s in zip(requirements, exact, fulltext, semantic, strict=True)
    ]
    for bundle in bundles:
        result.rankings[bundle.requirement.id] = {
            "exact": [back[e] for e, _ in bundle.exact],
            "fulltext": [back[e] for e, _ in bundle.fulltext],
            "semantic": [back[e] for e, _ in bundle.semantic],
            "hybrid": [back[e] for e, _ in fuse([bundle.exact, bundle.fulltext, bundle.semantic])],
        }
    started = time.perf_counter()
    selection = select_hybrid(account.facts, bundles, top_k=top_k, budget_chars=budget_chars)
    result.timings_ms["selection"] = (time.perf_counter() - started) * 1000
    by_id = {f.evidence_id: f for f in account.facts}
    result.selection = {
        "hybrid": [back[e] for e in selection.selected],
        "why": {back[e]: why for e, why in selection.why.items()},
        "dropped_for_budget": selection.dropped_for_budget,
        "chars_hybrid": sum(by_id[e].cost for e in selection.selected),
        "chars_full": sum(f.cost for f in account.facts),
        "facts_full": len(account.facts),
    }
    return result


# --- Aggregation -------------------------------------------------------------------


def labels_in(req: RequirementIn, available: set[str]) -> tuple[set[str], set[str]]:
    return set(req.direct) & available, set(req.related) & available


def score(dataset: Dataset, results: Sequence[ScenarioResult]) -> dict[str, object]:
    postings = {p.id: p for p in dataset.postings}
    per_scenario: dict[str, dict[str, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    per_role: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    selection_rows = []
    missing_noise: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for result in results:
        posting = postings[result.posting]
        profile = COMBINED if result.scenario == COMBINED else posting.profile
        available = {f.id for f in dataset.facts_for(profile)}
        pairs = covered_hybrid = 0
        direct_reqs = direct_reqs_covered = 0
        chosen = set(result.selection["hybrid"])  # type: ignore[arg-type]
        for req in posting.requirements:
            direct, related = labels_in(req, available)
            relevant = direct | related
            ranks = result.rankings[req.id]
            if not relevant:
                counts[result.scenario]["missing"] += 1
                for method in METHODS:
                    missing_noise[result.scenario][method].append(len(ranks[method]))
                continue
            counts[result.scenario]["labeled"] += 1
            pairs += len(relevant)
            covered_hybrid += len(relevant & chosen)
            if direct:
                direct_reqs += 1
                direct_reqs_covered += 1 if direct & chosen else 0
            bucket = per_scenario[result.scenario]
            for method in METHODS:
                ranked = ranks[method]
                for k in KS:
                    bucket[method][f"recall@{k}"].append(recall_at(ranked, relevant, k))
                for k in (3, 5):
                    bucket[method][f"precision@{k}"].append(precision_at(ranked, relevant, k))
                if direct:
                    for k in (1, 3, 5):
                        bucket[method][f"direct_hit@{k}"].append(hit_at(ranked, direct, k))
                    bucket[method]["mrr"].append(reciprocal_rank(ranked, direct))
                if result.scenario == COMBINED:
                    per_role[f"{posting.role}|{method}"]["recall@5"].append(
                        recall_at(ranked, relevant, 5)
                    )
        facts_full = int(result.selection["facts_full"])  # type: ignore[arg-type]
        selection_rows.append(
            {
                "posting": result.posting,
                "role": result.role,
                "scenario": result.scenario,
                "facts_full": facts_full,
                "facts_hybrid": len(chosen),
                "chars_full": result.selection["chars_full"],
                "chars_hybrid": result.selection["chars_hybrid"],
                "pair_coverage_hybrid": round(covered_hybrid / pairs, 4) if pairs else None,
                "direct_req_coverage_hybrid": (
                    round(direct_reqs_covered / direct_reqs, 4) if direct_reqs else None
                ),
                "missed_pairs": pairs - covered_hybrid,
                "dropped_for_budget": result.selection["dropped_for_budget"],
            }
        )
    rank_metrics = {
        scenario: {
            method: {name: mean(values) for name, values in sorted(metrics.items())}
            for method, metrics in methods.items()
        }
        for scenario, methods in per_scenario.items()
    }
    timings: dict[str, dict[str, float | None]] = defaultdict(dict)
    for scenario in {r.scenario for r in results}:
        rows = [r for r in results if r.scenario == scenario]
        for name in rows[0].timings_ms:
            timings[scenario][name] = round(statistics.median(r.timings_ms[name] for r in rows), 2)
    return {
        "counts": counts,
        "rank_metrics": rank_metrics,
        "recall5_by_role_combined": {
            key: mean(values["recall@5"]) for key, values in sorted(per_role.items())
        },
        "missing_requirement_results": {
            scenario: {method: mean(values) for method, values in methods.items()}
            for scenario, methods in missing_noise.items()
        },
        "selection": selection_rows,
        "median_timings_ms_per_posting": timings,
    }
