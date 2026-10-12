"""Keep a user's fact embeddings in step with their confirmed facts.

Each confirmed fact version becomes one or more chunks: the fact text (split
into overlapping token windows only if it exceeds the model's input limit)
with its parent context (entry and section) in front, so "Built dashboards"
is embedded as work done in that role. Only facts whose (id, version) has no
chunks for the current model revision are embedded; identical text the same
user already embedded reuses that vector. Chunks of old versions, rejected
facts, and other model revisions are deleted. Everything is scoped to one
owner, inside that owner's row-level-security session.

Indexing runs in three steps so CPU work never holds a database transaction
open: ``plan_sync`` (read), ``embed_plan`` (no database), ``apply_sync``
(write; idempotent, so a crash or a concurrent job is harmless).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, delete, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import EvidenceChunk
from app.retrieval.embedder import Embedder

# Room for [CLS]/[SEP] and the newline; windows overlap so a phrase split at a
# boundary survives in one of them.
RESERVED_TOKENS = 3
WINDOW_OVERLAP = 32
MIN_WINDOW_TOKENS = 32
MAX_CONTEXT_CHARS = 300


@dataclass(frozen=True)
class FactRef:
    """The parts of a confirmed fact version that indexing needs."""

    evidence_id: uuid.UUID
    version: int
    text: str
    section: str | None
    entry: str | None
    experience_type: str


@dataclass
class SyncPlan:
    facts: int
    stale: list[tuple[uuid.UUID, int]]
    rows: list[dict[str, Any]] = field(default_factory=list)
    # Distinct texts that need a new vector, in order.
    texts: list[str] = field(default_factory=list)
    vectors: dict[str, list[float]] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncStats:
    facts: int
    embedded: int
    reused: int
    deleted: int


def chunk_context(fact: FactRef) -> str:
    parts = [p for p in (fact.entry, fact.section) if p]
    label = " | ".join(parts) if parts else "Profile"
    return f"{label} ({fact.experience_type})"[:MAX_CONTEXT_CHARS]


def embed_text(context: str, body: str) -> str:
    return f"{context}\n{body}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def chunk_fact(embedder: Embedder, fact: FactRef) -> list[tuple[str, str]]:
    """(context, body) for each chunk of a fact, each within the model's input limit."""
    context = chunk_context(fact)
    budget = embedder.max_tokens - RESERVED_TOKENS - embedder.count_tokens(context)
    bodies = embedder.windows(fact.text.strip(), max(budget, MIN_WINDOW_TOKENS), WINDOW_OVERLAP)
    return [(context, body) for body in bodies]


def _model_filter(owner_id: uuid.UUID, embedder: Embedder):  # noqa: ANN202
    return and_(
        EvidenceChunk.owner_id == owner_id,
        EvidenceChunk.model == embedder.model_id,
        EvidenceChunk.model_revision == embedder.revision,
    )


def plan_sync(
    db: Session, owner_id: uuid.UUID, facts: list[FactRef], embedder: Embedder
) -> SyncPlan:
    existing = db.execute(
        select(
            EvidenceChunk.evidence_id,
            EvidenceChunk.evidence_version,
            EvidenceChunk.content_sha256,
            EvidenceChunk.embedding,
        ).where(_model_filter(owner_id, embedder))
    ).all()
    indexed = {(row.evidence_id, row.evidence_version) for row in existing}
    known = {row.content_sha256: [float(x) for x in row.embedding] for row in existing}
    current = {(f.evidence_id, f.version) for f in facts}
    plan = SyncPlan(facts=len(facts), stale=sorted(indexed - current, key=str))

    queued: set[str] = set()
    for fact in facts:
        if (fact.evidence_id, fact.version) in indexed:
            continue
        for index, (context, body) in enumerate(chunk_fact(embedder, fact)):
            text = embed_text(context, body)
            digest = sha256_text(text)
            plan.rows.append(
                {
                    "owner_id": owner_id,
                    "evidence_id": fact.evidence_id,
                    "evidence_version": fact.version,
                    "chunk_index": index,
                    "context": context,
                    "body": body,
                    "content_sha256": digest,
                    "token_count": embedder.count_tokens(text),
                    "model": embedder.model_id,
                    "model_revision": embedder.revision,
                    "dimension": embedder.dimension,
                    "normalized": True,
                    "embedding": known.get(digest),
                    "_text": text,
                }
            )
            if digest not in known and digest not in queued:
                queued.add(digest)
                plan.texts.append(text)
    return plan


def embed_plan(plan: SyncPlan, embedder: Embedder) -> None:
    plan.vectors = dict(zip(plan.texts, embedder.embed(plan.texts), strict=True))


def apply_sync(db: Session, owner_id: uuid.UUID, plan: SyncPlan, embedder: Embedder) -> SyncStats:
    deleted = 0
    if plan.stale:
        deleted += db.execute(
            delete(EvidenceChunk).where(
                _model_filter(owner_id, embedder),
                tuple_(EvidenceChunk.evidence_id, EvidenceChunk.evidence_version).in_(plan.stale),
            )
        ).rowcount
    # Chunks from another model or revision are never searched; drop them.
    deleted += db.execute(
        delete(EvidenceChunk).where(
            EvidenceChunk.owner_id == owner_id,
            or_(
                EvidenceChunk.model != embedder.model_id,
                EvidenceChunk.model_revision != embedder.revision,
            ),
        )
    ).rowcount
    rows, reused = [], 0
    for planned in plan.rows:
        row = {k: v for k, v in planned.items() if k != "_text"}
        if row["embedding"] is None:
            row["embedding"] = plan.vectors[planned["_text"]]
        else:
            reused += 1
        rows.append(row)
    if rows:
        # A concurrent job may have indexed the same version first: keep its rows.
        db.execute(insert(EvidenceChunk).values(rows).on_conflict_do_nothing())
    return SyncStats(plan.facts, len(plan.texts), reused, deleted)


def sync_chunks(
    db: Session, owner_id: uuid.UUID, facts: list[FactRef], embedder: Embedder
) -> SyncStats:
    """All three steps in one session (tests and small batches)."""
    plan = plan_sync(db, owner_id, facts, embedder)
    embed_plan(plan, embedder)
    return apply_sync(db, owner_id, plan, embedder)
