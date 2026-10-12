"""Decide and record which confirmed facts a draft's model context uses.

``retrieve_for_draft`` is called by the drafting job (worker). It picks a mode,
reuses a stored selection when nothing relevant changed (tenant-scoped cache:
same owner, analysis, fact versions, and configuration), and otherwise runs
hybrid search and stores the result as an insert-only ``retrievals`` row.

Modes and reasons (stored with every row):
- full / configured: RETRIEVAL_MODE=full.
- full / no_requirements: the analysis lists nothing to search for.
- full / small_profile: RETRIEVAL_MODE=auto and every confirmed fact fits
  RETRIEVAL_FULL_CONTEXT_MAX_CHARS, so nothing would be gained by leaving
  facts out.
- full / embeddings_unavailable: the local model is missing or failed to load
  (logged); full context is the safe fallback.
- hybrid / relevance: per-requirement search and the selection policy.

Embeddings run locally: no fact text leaves the server for retrieval. CPU work
(model loading, embedding) never happens inside a database transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.auth.dependencies import user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import EvidenceRecord, EvidenceVersion, Retrieval
from app.retrieval import index
from app.retrieval.embedder import (
    MODEL_ID,
    MODEL_REVISION,
    Embedder,
    EmbeddingUnavailableError,
    load_embedder,
)
from app.retrieval.search import (
    Candidate,
    exact_ranking,
    fulltext_ranking,
    query_text,
    semantic_ranking,
)
from app.retrieval.selection import (
    STRATEGY_VERSION,
    ProfileFact,
    RequirementRankings,
    Selection,
    full_selection,
    select_hybrid,
)
from app.tailoring.models import AnalysisContent

logger = logging.getLogger("app.retrieval")

Rows = Sequence[tuple[EvidenceRecord, EvidenceVersion]]
EmbedderLoader = Callable[[Settings], Embedder]


@dataclass(frozen=True)
class RetrievalOutcome:
    retrieval_id: uuid.UUID
    mode: str
    reason: str
    selected: frozenset[uuid.UUID]
    reused: bool


def ordered_verified(rows: Rows) -> list[tuple[EvidenceRecord, EvidenceVersion]]:
    """Confirmed facts in profile order (the order build_context uses)."""
    verified = [(r, v) for r, v in rows if v.verification_status == "verified"]
    verified.sort(
        key=lambda rv: (rv[0].origin != "master", str(rv[0].master_version_id), rv[0].position)
    )
    return verified


def profile_facts(rows: Rows) -> list[ProfileFact]:
    return [
        ProfileFact(r.id, v.text, v.section, v.entry, v.date_text, v.experience_type)
        for r, v in ordered_verified(rows)
    ]


def profile_fingerprint(rows: Rows) -> str:
    keys = sorted(f"{r.id}:{v.version}" for r, v in ordered_verified(rows))
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def config_fingerprint(settings: Settings, embedding_revision: str) -> str:
    config = {
        "strategy": STRATEGY_VERSION,
        "mode": settings.retrieval_mode,
        "full_context_max_chars": settings.retrieval_full_context_max_chars,
        "top_k": settings.retrieval_top_k,
        "max_context_chars": settings.max_context_chars,
        "embedding": f"{MODEL_ID}@{embedding_revision}",
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def _chars_within(facts: Sequence[ProfileFact], ids: set[uuid.UUID], budget: int) -> tuple:
    """(facts, chars) the context will actually hold, applying build_context's budget."""
    count = chars = 0
    for fact in facts:
        if fact.evidence_id not in ids:
            continue
        if chars + fact.cost > budget:
            break
        count += 1
        chars += fact.cost
    return count, chars


def _hybrid(
    settings: Settings,
    user: VerifiedUser,
    analysis: AnalysisContent,
    rows: Rows,
    facts: list[ProfileFact],
    embedder: Embedder,
) -> tuple[Selection, list[RequirementRankings]]:
    refs = [
        index.FactRef(r.id, v.version, v.text, v.section, v.entry, v.experience_type)
        for r, v in ordered_verified(rows)
    ]
    with user_db(settings, user) as db:
        plan = index.plan_sync(db, user.id, refs, embedder)
    index.embed_plan(plan, embedder)
    queries = embedder.embed([query_text(r) for r in analysis.requirements])
    candidates = [Candidate(f.evidence_id, f.text) for f in facts]
    rankings: list[RequirementRankings] = []
    with user_db(settings, user) as db:
        stats = index.apply_sync(db, user.id, plan, embedder)
        for requirement, vector in zip(analysis.requirements, queries, strict=True):
            rankings.append(
                RequirementRankings(
                    requirement,
                    exact_ranking(requirement, candidates),
                    fulltext_ranking(db, user.id, requirement),
                    semantic_ranking(db, user.id, embedder, vector),
                )
            )
    logger.info(
        "retrieval index facts=%d embedded=%d reused=%d deleted=%d",
        stats.facts,
        stats.embedded,
        stats.reused,
        stats.deleted,
    )
    selection = select_hybrid(
        facts, rankings, top_k=settings.retrieval_top_k, budget_chars=settings.max_context_chars
    )
    return selection, rankings


def retrieve_for_draft(
    settings: Settings,
    user: VerifiedUser,
    run_id: uuid.UUID,
    analysis_id: uuid.UUID,
    analysis: AnalysisContent,
    rows: Rows,
    loader: EmbedderLoader | None = None,
) -> RetrievalOutcome:
    started = time.perf_counter()
    facts = profile_facts(rows)
    chars_total = sum(f.cost for f in facts)
    embedder: Embedder | None = None
    revision = MODEL_REVISION
    if settings.retrieval_mode == "full":
        reason = "configured"
    elif not analysis.requirements:
        reason = "no_requirements"
    elif (
        settings.retrieval_mode == "auto"
        and chars_total <= settings.retrieval_full_context_max_chars
    ):
        reason = "small_profile"
    else:
        try:
            embedder = (loader or load_embedder)(settings)
            reason = "relevance"
        except EmbeddingUnavailableError:
            logger.warning("retrieval falling back to full context: embeddings unavailable")
            reason, revision = "embeddings_unavailable", "unavailable"
    key = (profile_fingerprint(rows), config_fingerprint(settings, revision))

    with user_db(settings, user) as db:
        cached = db.scalars(
            select(Retrieval).where(
                Retrieval.owner_id == user.id,
                Retrieval.analysis_id == analysis_id,
                Retrieval.profile_fingerprint == key[0],
                Retrieval.config_fingerprint == key[1],
            )
        ).first()
        if cached is not None:
            return RetrievalOutcome(
                cached.id,
                cached.mode,
                cached.reason,
                frozenset(uuid.UUID(e) for e in cached.result.get("selected", [])),
                reused=True,
            )

    rankings: list[RequirementRankings] = []
    if embedder is None:
        selection = full_selection(facts, reason)
    else:
        selection, rankings = _hybrid(settings, user, analysis, rows, facts, embedder)
    selected = set(selection.selected)
    count, chars = _chars_within(facts, selected, settings.max_context_chars)
    runtime_ms = int((time.perf_counter() - started) * 1000)
    with user_db(settings, user) as db:
        db.execute(
            insert(Retrieval)
            .values(
                owner_id=user.id,
                run_id=run_id,
                analysis_id=analysis_id,
                mode=selection.mode,
                reason=selection.reason,
                strategy_version=STRATEGY_VERSION,
                profile_fingerprint=key[0],
                config_fingerprint=key[1],
                embedding_model=MODEL_ID if embedder else None,
                embedding_revision=embedder.revision if embedder else None,
                facts_total=len(facts),
                facts_selected=count,
                chars_total=chars_total,
                chars_selected=chars,
                runtime_ms=runtime_ms,
                result=selection.to_json(rankings),
            )
            .on_conflict_do_nothing(constraint="uq_retrievals_cache_key")
        )
        stored = db.scalars(
            select(Retrieval).where(
                Retrieval.owner_id == user.id,
                Retrieval.analysis_id == analysis_id,
                Retrieval.profile_fingerprint == key[0],
                Retrieval.config_fingerprint == key[1],
            )
        ).one()
        logger.info(
            "retrieval mode=%s reason=%s facts=%d/%d runtime_ms=%d",
            stored.mode,
            stored.reason,
            stored.facts_selected,
            stored.facts_total,
            stored.runtime_ms,
        )
        return RetrievalOutcome(
            stored.id,
            stored.mode,
            stored.reason,
            frozenset(uuid.UUID(e) for e in stored.result.get("selected", [])),
            reused=False,
        )
