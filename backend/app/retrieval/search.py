"""Per-requirement search over one user's confirmed facts (milestone 7, ADR 0010).

Three rankings, each answering "which facts speak to this requirement?":

- exact: the requirement's posting terms found in the fact as whole words in
  an affirmative clause (the ATS matcher's rules: Java never matches
  JavaScript, "no Tableau experience" does not count).
- fulltext: Postgres full-text search (English stemming, ts_rank_cd) of the
  requirement's words against the fact text.
- semantic: cosine similarity between the requirement and the fact's
  embedded chunks (pgvector, exact scan: no approximate index until
  measurement shows a need).

``hybrid`` merges them with Reciprocal Rank Fusion: score = sum over the
rankings of 1 / (RRF_K + rank). It needs no score calibration between methods,
and a fact found by several methods rises. Every query filters by owner and
also runs under row-level security; user text only ever reaches SQL as bound
parameters.

A high similarity means "about the same topic", never "true": retrieval only
chooses what the model may read. Every claim is still checked against the
cited facts afterwards (app/tailoring/validate.py).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import Float, and_, bindparam, func, literal_column, select
from sqlalchemy.orm import Session

from app.ats.matching import STOPWORDS, clean, find_term
from app.db.models import EvidenceChunk, EvidenceRecord, EvidenceVersion
from app.retrieval.embedder import Embedder
from app.tailoring.models import Requirement

RRF_K = 60
# How deep each ranking is read before fusion.
CANDIDATES = 20
MAX_QUERY_WORDS = 24
_QUERY_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")


@dataclass(frozen=True)
class Candidate:
    """A confirmed fact as search sees it (current version only)."""

    evidence_id: uuid.UUID
    text: str


Ranking = list[tuple[uuid.UUID, float]]


def query_text(requirement: Requirement) -> str:
    """What is embedded for a requirement: its sentence plus any terms it does not repeat."""
    extra = [t for t in requirement.terms if t.lower() not in requirement.text.lower()]
    return requirement.text + (f" ({', '.join(extra)})" if extra else "")


def fulltext_query(requirement: Requirement) -> str | None:
    """An OR query of the requirement's meaningful words, for websearch_to_tsquery.

    Words are reduced to plain letters and digits, so nothing the posting says
    can act as query syntax; the string is still passed as a bound parameter.
    """
    words: list[str] = []
    for word in _QUERY_WORD.findall(" ".join([requirement.text, *requirement.terms])):
        lower = word.lower()
        if len(lower) < 2 or lower in STOPWORDS or lower == "or" or lower in words:
            continue
        words.append(lower)
    return " or ".join(words[:MAX_QUERY_WORDS]) or None


def exact_ranking(requirement: Requirement, candidates: Iterable[Candidate]) -> Ranking:
    """Facts naming the most of the requirement's terms affirmatively, best first."""
    scored: list[tuple[uuid.UUID, float]] = []
    for candidate in candidates:
        text = clean(candidate.text)
        hits = sum(
            1 for term in requirement.terms if any(not hit.negated for hit in find_term(text, term))
        )
        if hits:
            scored.append((candidate.evidence_id, float(hits)))
    scored.sort(key=lambda item: -item[1])  # stable: profile order breaks ties
    return scored


def _current_verified(owner_id: uuid.UUID):  # noqa: ANN202
    return and_(
        EvidenceRecord.owner_id == owner_id,
        EvidenceVersion.verification_status == "verified",
    )


def fulltext_ranking(
    db: Session, owner_id: uuid.UUID, requirement: Requirement, limit: int = CANDIDATES
) -> Ranking:
    query = fulltext_query(requirement)
    if query is None:
        return []
    tsquery = func.websearch_to_tsquery(literal_column("'english'"), bindparam("q", query))
    vector = func.to_tsvector(literal_column("'english'"), EvidenceVersion.text)
    rank = func.ts_rank_cd(vector, tsquery).label("rank")
    rows = db.execute(
        select(EvidenceRecord.id, rank)
        .join(
            EvidenceVersion,
            and_(
                EvidenceVersion.evidence_id == EvidenceRecord.id,
                EvidenceVersion.version == EvidenceRecord.current_version,
            ),
        )
        .where(_current_verified(owner_id), vector.op("@@")(tsquery))
        .order_by(rank.desc(), EvidenceRecord.position)
        .limit(limit)
    ).all()
    return [(row.id, float(row.rank)) for row in rows]


def semantic_ranking(
    db: Session,
    owner_id: uuid.UUID,
    embedder: Embedder,
    query_vector: Sequence[float],
    limit: int = CANDIDATES,
) -> Ranking:
    """Facts by their best chunk's cosine similarity to the query (exact search)."""
    similarity = func.max(
        1 - EvidenceChunk.embedding.cosine_distance(query_vector), type_=Float
    ).label("similarity")
    rows = db.execute(
        select(EvidenceChunk.evidence_id, similarity)
        .join(
            EvidenceRecord,
            and_(
                EvidenceRecord.id == EvidenceChunk.evidence_id,
                EvidenceRecord.owner_id == EvidenceChunk.owner_id,
                EvidenceRecord.current_version == EvidenceChunk.evidence_version,
            ),
        )
        .join(
            EvidenceVersion,
            and_(
                EvidenceVersion.evidence_id == EvidenceChunk.evidence_id,
                EvidenceVersion.version == EvidenceChunk.evidence_version,
            ),
        )
        .where(
            _current_verified(owner_id),
            EvidenceChunk.owner_id == owner_id,
            EvidenceChunk.model == embedder.model_id,
            EvidenceChunk.model_revision == embedder.revision,
        )
        .group_by(EvidenceChunk.evidence_id)
        .order_by(similarity.desc())
        .limit(limit)
    ).all()
    return [(row.evidence_id, float(row.similarity)) for row in rows]


def fuse(rankings: Sequence[Ranking], k: int = RRF_K) -> Ranking:
    """Reciprocal Rank Fusion; ties keep the order of first appearance."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, (evidence_id, _) in enumerate(ranking, start=1):
            scores[evidence_id] = scores.get(evidence_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: -item[1])
