"""Milestone 7 units: chunking, query building, rankings, fusion, selection, metrics."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.ai.schemas import TailoredDraftOut
from app.config import Settings
from app.retrieval import embedder as embedder_module
from app.retrieval.index import FactRef, chunk_context, chunk_fact
from app.retrieval.search import (
    Candidate,
    exact_ranking,
    fulltext_query,
    fuse,
    query_text,
)
from app.retrieval.selection import (
    ProfileFact,
    RequirementRankings,
    full_selection,
    select_hybrid,
)
from app.retrieval.service import full_context_reason
from app.tailoring.context import build_context, subset_context
from app.tailoring.models import AnalysisContent, Requirement
from app.tailoring.validate import check, finalize
from evaluation.retrieval import (
    Dataset,
    hit_at,
    load_dataset,
    precision_at,
    recall_at,
    reciprocal_rank,
)
from tests.retrieval_fakes import HashEmbedder


def req(id_: str = "R1", text: str = "Experience with SQL", terms=("SQL",), **kw) -> Requirement:
    return Requirement(
        id=id_,
        category=kw.get("category", "required"),
        text=text,
        terms=list(terms),
        mandatory=kw.get("mandatory", False),
    )


def fact(text: str, **kw) -> ProfileFact:
    return ProfileFact(
        kw.get("evidence_id", uuid.uuid4()),
        text,
        kw.get("section", "Experience"),
        kw.get("entry", "Analyst, Example Co"),
        kw.get("date_text", "2021 - 2023"),
        kw.get("experience_type", "professional"),
    )


# --- Chunking -----------------------------------------------------------------------


def test_short_fact_is_one_chunk_with_parent_context():
    ref = FactRef(
        uuid.uuid4(), 1, "Built dashboards", "Experience", "Analyst, Example Co", "professional"
    )
    chunks = chunk_fact(HashEmbedder(), ref)
    assert chunks == [("Analyst, Example Co | Experience (professional)", "Built dashboards")]


def test_skill_line_without_entry_uses_section_as_context():
    ref = FactRef(uuid.uuid4(), 1, "SQL, Python", "Skills", None, "professional")
    assert chunk_context(ref) == "Skills (professional)"


def test_long_fact_splits_into_overlapping_windows_within_the_model_limit():
    embedder = HashEmbedder(max_tokens=40)
    words = [f"w{i}" for i in range(100)]
    ref = FactRef(uuid.uuid4(), 1, " ".join(words), None, None, "other")
    chunks = chunk_fact(embedder, ref)
    assert len(chunks) > 1
    for context, body in chunks:
        assert embedder.count_tokens(f"{context}\n{body}") <= 40
    # Overlap: each window starts before the previous one ended.
    first, second = chunks[0][1].split(), chunks[1][1].split()
    assert set(first) & set(second)
    covered = {w for _, body in chunks for w in body.split()}
    assert covered == set(words)


# --- Queries and rankings ----------------------------------------------------------------


def test_fulltext_query_is_plain_words_joined_with_or():
    hostile = req(text='SQL\'); DROP TABLE app.evidence_records; -- "quoted" -excluded', terms=())
    query = fulltext_query(hostile)
    assert query is not None
    for word in query.split(" or "):
        assert word.isalnum()
    assert "drop" in query and "'" not in query and "-" not in query and '"' not in query


def test_fulltext_query_none_for_stopwords_only():
    assert fulltext_query(req(text="the ability to work with", terms=())) is None


def test_query_text_adds_terms_not_in_the_sentence():
    assert query_text(req(text="Dashboard experience", terms=("Tableau",))) == (
        "Dashboard experience (Tableau)"
    )
    assert query_text(req(text="Tableau dashboards", terms=("Tableau",))) == "Tableau dashboards"


def test_exact_ranking_avoids_compound_and_substring_traps():
    rd = Candidate(uuid.uuid4(), "Supported the R&D team with product testing data.")
    r_lang = Candidate(uuid.uuid4(), "Built a churn analysis in R.")
    js = Candidate(uuid.uuid4(), "Built charts with JavaScript.")
    negated = Candidate(uuid.uuid4(), "Reports were built without SQL access.")
    sql = Candidate(uuid.uuid4(), "Wrote SQL and Python reports.")
    found_r = exact_ranking(req(text="R or SAS", terms=("R", "SAS")), [rd, r_lang])
    assert [e for e, _ in found_r] == [r_lang.evidence_id]
    assert exact_ranking(req(text="Java", terms=("Java",)), [js]) == []
    found = exact_ranking(req(terms=("SQL", "Python")), [negated, sql])
    assert found == [(sql.evidence_id, 2.0)]


def test_reciprocal_rank_fusion_rewards_agreement():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fused = fuse([[(a, 1.0), (b, 0.5)], [(b, 3.0)], [(c, 0.9), (b, 0.8)]])
    assert [e for e, _ in fused] == [b, a, c]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61 + 1 / 62)


# --- Selection policy ------------------------------------------------------------------


def test_selection_keeps_top_k_essentials_and_profile_order():
    sql, tableau, cooking = fact("Wrote SQL"), fact("Tableau dashboards"), fact("Cooked lunch")
    degree = fact(
        "B.S. Statistics", section="Education", entry="B.S., Example U", experience_type="academic"
    )
    facts = [sql, tableau, cooking, degree]
    rankings = [
        RequirementRankings(
            req(),
            [(sql.evidence_id, 1.0)],
            [],
            [(sql.evidence_id, 0.9), (cooking.evidence_id, 0.1)],
        ),
    ]
    selection = select_hybrid(facts, rankings, top_k=1, budget_chars=10_000)
    assert selection.mode == "hybrid"
    assert selection.selected == [sql.evidence_id, degree.evidence_id]
    assert selection.why[degree.evidence_id] == "essential"
    assert selection.why[sql.evidence_id] == "requirement:R1"


def test_exact_fallback_adds_a_term_match_fusion_missed():
    noise = [fact(f"Noise {i}", entry=f"Job {i}") for i in range(3)]
    hit = fact("Tuned Snowflake warehouses", entry="Job 9")
    rankings = [
        RequirementRankings(
            req(text="Snowflake", terms=("Snowflake",)),
            [(hit.evidence_id, 1.0)],
            [(n.evidence_id, 1.0) for n in noise],
            [(n.evidence_id, 0.9) for n in noise],
        )
    ]
    selection = select_hybrid([*noise, hit], rankings, top_k=1, budget_chars=10_000)
    assert hit.evidence_id in selection.selected


def test_every_job_keeps_at_least_one_fact():
    job_a = [fact("SQL reports", entry="Analyst, A"), fact("Excel", entry="Analyst, A")]
    job_b = [
        fact("Answered phones", entry="Receptionist, B"),
        fact("Filed records", entry="Receptionist, B"),
    ]
    personal = fact("Hiked trails", entry="Hobby", experience_type="personal")
    rankings = [RequirementRankings(req(), [(job_a[0].evidence_id, 1.0)], [], [])]
    selection = select_hybrid([*job_a, *job_b, personal], rankings, top_k=1, budget_chars=10_000)
    kept_b = [f for f in job_b if f.evidence_id in selection.selected]
    assert len(kept_b) == 1 and selection.why[kept_b[0].evidence_id] == "entry_coverage"
    assert personal.evidence_id not in selection.selected  # coverage is for jobs and internships


def test_budget_drops_lowest_ranked_never_essentials():
    degree = fact("D" * 100, section="Education", entry="Degree", experience_type="academic")
    strong, weak = fact("S" * 100, entry="Job"), fact("W" * 100, entry="Job")
    rankings = [
        RequirementRankings(req(), [(strong.evidence_id, 2.0), (weak.evidence_id, 1.0)], [], [])
    ]
    budget = degree.cost + strong.cost
    selection = select_hybrid([degree, strong, weak], rankings, top_k=2, budget_chars=budget)
    assert selection.selected == [degree.evidence_id, strong.evidence_id]
    assert selection.dropped_for_budget == 1


def test_selection_json_holds_ids_and_scores_only():
    secret = fact("Confidential project codename Falcon")
    rankings = [RequirementRankings(req(), [(secret.evidence_id, 1.0)], [], [])]
    stored = json.dumps(
        select_hybrid([secret], rankings, top_k=1, budget_chars=1000).to_json(rankings)
    )
    assert "Falcon" not in stored and str(secret.evidence_id) in stored


def test_full_selection_lists_every_fact():
    facts = [fact("a"), fact("b")]
    selection = full_selection(facts, "small_profile")
    assert selection.mode == "full" and selection.selected == [f.evidence_id for f in facts]


@pytest.mark.parametrize(
    ("overrides", "has_requirements", "chars", "expected"),
    [
        ({"retrieval_mode": "full"}, True, 999_999, "configured"),
        ({}, False, 999_999, "no_requirements"),
        ({}, True, 60_000, "small_profile"),  # fits the drafting budget: send it all
        ({}, True, 60_001, None),  # would be cut by position: retrieve instead
        ({"max_context_chars": 20_000}, True, 30_000, None),  # the smaller limit wins
        ({"retrieval_full_context_max_chars": 1000}, True, 1001, None),
        ({"retrieval_mode": "hybrid"}, True, 10, None),
    ],
)
def test_auto_mode_retrieves_only_when_the_profile_would_not_fit(
    overrides, has_requirements, chars, expected
):
    settings = Settings(_env_file=None, **overrides)
    assert full_context_reason(settings, has_requirements, chars) == expected


# --- Context subset and omissions ----------------------------------------------------------


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _rows(texts_by_entry):
    rows = []
    for position, (entry, text) in enumerate(texts_by_entry):
        record = _Row(id=uuid.uuid4(), origin="master", master_version_id=None, position=position)
        version = _Row(
            text=text,
            section="Experience",
            entry=entry,
            date_text="2020",
            experience_type="professional",
            verification_status="verified",
            version=1,
        )
        rows.append((record, version))
    return rows


def test_subset_keeps_aliases_and_unseen_facts_are_reported_as_left_out():
    rows = _rows(
        [("Analyst, A", "Wrote SQL reports"), ("Analyst, A", "Ran payroll"), ("Clerk, B", "Filed")]
    )
    everything = build_context(rows, 10**9)
    keep = {rows[0][0].id, rows[2][0].id}
    subset = subset_context(everything, keep)
    assert set(subset.facts) == {"E1", "E3"}
    assert subset.facts["E3"] is everything.facts["E3"]
    assert set(subset.entries) == {"J1", "J2"} and len(subset.entries["J1"].facts) == 1

    analysis = AnalysisContent(role_title="Analyst", requirements=[req()])
    draft = TailoredDraftOut.model_validate(
        {
            "headline": "Analyst",
            "summary": None,
            "entries": [
                {
                    "entry_ref": "J1",
                    "bullets": [
                        {
                            "text": "Wrote SQL reports",
                            "evidence_ids": ["E1"],
                            "requirement_ids": ["R1"],
                        }
                    ],
                }
            ],
            "skills": [],
            "omitted_evidence_ids": [],
        }
    )
    checked = check(draft, subset, analysis)
    content, _ = finalize(draft, subset, analysis, checked, False, everything)
    left_out = {o.evidence_id for o in content.omitted}
    assert left_out == {rows[1][0].id, rows[2][0].id}  # unseen E2 and unused E3


# --- Model file verification ------------------------------------------------------------------


def test_snapshot_verification_rejects_missing_or_altered_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(embedder_module, "FILES", {"vocab.txt": "0" * 64})
    with pytest.raises(embedder_module.EmbeddingUnavailableError, match="missing"):
        embedder_module.verify_snapshot(tmp_path)
    (tmp_path / "vocab.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(embedder_module.EmbeddingUnavailableError, match="pinned hash"):
        embedder_module.verify_snapshot(tmp_path)


# --- Evaluation set and metrics ------------------------------------------------------------------


def test_labeled_set_is_consistent_and_covers_four_roles():
    dataset = load_dataset()
    assert {p.role for p in dataset.postings} == {"Data", "GRC", "Business Analyst", "Tech Sales"}
    assert sum(len(f) for f in dataset.profiles.values()) == 78
    missing = [r for p in dataset.postings for r in p.requirements if not r.direct + r.related]
    assert len(missing) >= 8  # requirements with no evidence anywhere


def test_labeled_set_rejects_unknown_or_duplicate_labels():
    raw = json.loads(load_dataset().model_dump_json())
    raw["postings"][0]["requirements"][0]["direct"].append("zz99")
    with pytest.raises(ValueError, match="unknown facts"):
        Dataset.model_validate(raw)


def test_ranking_metrics():
    ranked = ["a", "x", "b", "y"]
    relevant = {"a", "b", "c"}
    assert recall_at(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert precision_at(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert hit_at(ranked, {"b"}, 1) == 0.0 and hit_at(ranked, {"b"}, 3) == 1.0
    assert reciprocal_rank(ranked, {"b"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(ranked, {"z"}) == 0.0
