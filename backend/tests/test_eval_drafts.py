"""The live draft comparison's guards and pure scoring (no database, no model)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.config import Settings
from app.tailoring.models import (
    DraftBullet,
    DraftContent,
    DraftEntry,
    DraftSkill,
    ValidationReport,
)
from evaluation.drafts import (
    DraftResult,
    blind_pairs,
    evaluate_draft,
    job_description,
    labeled_analysis,
    worst_case_per_draft,
)
from evaluation.retrieval import load_dataset
from scripts import eval_drafts


def test_cli_requires_an_explicit_budget_or_demo(capsys):
    with pytest.raises(SystemExit):
        eval_drafts.main([])
    assert "--budget-usd" in capsys.readouterr().err


@pytest.mark.parametrize("budget", ["0", "-1", "5.01"])
def test_cli_rejects_budgets_outside_the_guard(monkeypatch, budget):
    monkeypatch.setattr(
        eval_drafts, "get_settings", lambda: Settings(_env_file=None, openai_api_key="sk-test")
    )
    with pytest.raises(SystemExit, match="budget-usd must be"):
        eval_drafts.main(["--budget-usd", budget])


def test_cli_refuses_a_remote_database(monkeypatch):
    monkeypatch.setattr(
        eval_drafts,
        "get_settings",
        lambda: Settings(_env_file=None, supabase_url="https://example.supabase.co"),
    )
    with pytest.raises(SystemExit, match="local Supabase"):
        eval_drafts.main(["--demo"])


def test_worst_case_covers_two_calls_at_the_output_limit():
    settings = Settings(_env_file=None)
    small, large = worst_case_per_draft(settings, 3000), worst_case_per_draft(settings, 12000)
    output_only = 2 * settings.draft_max_output_tokens * settings.price_output_per_mtok / 1_000_000
    assert large > small > Decimal(output_only)
    assert Decimal("1.00") / large > 32  # the authorized $1 covers every draft's worst case


def test_job_description_lists_requirements_under_headings():
    posting = load_dataset().postings[0]
    text = job_description(posting)
    assert text.startswith(f"{posting.company} is hiring a {posting.title}.")
    assert "Required qualifications:" in text
    assert all(f"- {r.text}" in text for r in posting.requirements)


def test_evaluate_draft_counts_cited_labeled_facts_and_validation():
    dataset = load_dataset()
    posting = dataset.postings[0]
    available = {f.id for f in dataset.facts_for(posting.profile)}
    direct = sorted({f for r in posting.requirements for f in r.direct} & available)
    ids = {fact_id: uuid.uuid4() for fact_id in direct}
    back = {str(v): k for k, v in ids.items()}
    first = direct[0]
    content = DraftContent(
        headline=posting.title,
        entries=[
            DraftEntry(
                ref="E1",
                section="Experience",
                entry="Fictional Co",
                date_text="2024",
                experience_type="professional",
                bullets=[DraftBullet(text="Built SQL reports", evidence_ids=[ids[first]])],
            )
        ],
        skills=[DraftSkill(text="SQL", evidence_ids=[ids[first]])],
        omitted=[],
    )
    report = ValidationReport(
        rule_version="v", issues=[], removed=["skill 2: x"], repaired=True, passed=True
    )
    out = evaluate_draft(content, report, posting, available, back, labeled_analysis(posting))
    assert out["direct_used"] == round(1 / len(direct), 4)
    assert out["repaired"] is True and out["removed"] == 1 and out["passed"] is True
    assert out["ats_score"] is None or 0 <= out["ats_score"] <= 100


def test_blind_pairs_hide_the_mode_and_keep_a_key():
    dataset = load_dataset()
    posting = dataset.postings[0]
    content = DraftContent(headline="Analyst", entries=[], skills=[], omitted=[]).model_dump(
        mode="json"
    )
    results = [
        DraftResult(posting.id, posting.role, "own", mode, "r", "ok", content=content)
        for mode in ("full", "hybrid")
    ]
    markdown, key = blind_pairs(results, dataset)
    assert set(key[f"{posting.id} / own"].values()) == {"full", "hybrid"}
    assert "### Draft A" in markdown and "### Draft B" in markdown
    assert "hybrid" not in markdown and "full context" not in markdown
