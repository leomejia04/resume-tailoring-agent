"""Automatic improvement rules (ADR 0008): which gaps confirmed facts support,
and when a candidate draft counts as an improvement.

Pure functions only; every resume line and posting here is fictional.
"""

import uuid

from app.ats.improve import MAX_TARGETS, fact_passages, judge, plan
from app.ats.passages import draft_passages
from app.ats.scoring import score
from app.render.format import FormatPreferences
from app.tailoring.context import Fact
from app.tailoring.models import (
    AnalysisContent,
    DraftBullet,
    DraftContent,
    DraftEntry,
    DraftSkill,
    Requirement,
)

JOB = "Requirements:\n- SQL\n- Tableau dashboards\n- Stakeholder reporting"


def req(rid, category, text, terms, mandatory=False):
    return Requirement(id=rid, category=category, text=text, terms=terms, mandatory=mandatory)


def analysis(*requirements):
    return AnalysisContent(role_title="Data Analyst", requirements=list(requirements))


def fact(alias, text, kind="professional", entry_ref="J1"):
    return Fact(
        alias=alias,
        evidence_id=uuid.uuid4(),
        text=text,
        section="experience" if entry_ref else "skills",
        entry="Data Analyst | Fictional Co." if entry_ref else None,
        date_text="2024",
        experience_type=kind,
        entry_ref=entry_ref,
    )


def draft(*bullets, skills=()):
    return DraftContent(
        headline="Data Analyst",
        summary=None,
        entries=[
            DraftEntry(
                ref="J1",
                section="experience",
                entry="Data Analyst | Fictional Co.",
                date_text="2024",
                experience_type="professional",
                bullets=[
                    DraftBullet(text=t, evidence_ids=[uuid.uuid4()], requirement_ids=[])
                    for t in bullets
                ],
            )
        ],
        skills=[DraftSkill(text=s, evidence_ids=[uuid.uuid4()]) for s in skills],
        omitted=[],
        format=FormatPreferences(show_headline=False),
    )


ANALYSIS = analysis(
    req("R1", "required", "SQL", ["SQL"], mandatory=True),
    req("R2", "required", "Tableau dashboards", ["Tableau"], mandatory=True),
    req("R3", "preferred", "Python scripting", ["Python"]),
)


def test_targets_only_requirements_confirmed_facts_support():
    facts = [
        fact("E1", "Wrote SQL queries for weekly reports."),
        fact("E2", "Built Tableau dashboards for the sales team."),
    ]
    result = plan(ANALYSIS, draft("Wrote SQL queries for weekly reports."), facts, JOB)
    assert [t.requirement_id for t in result.targets] == ["R2"]  # Python has no support
    assert result.targets[0].fact_aliases == ["E2"]
    assert result.open_gaps == 2  # R2 and R3 are both below full credit


def test_a_fact_the_draft_reworded_away_is_still_a_target():
    facts = [fact("E1", "Built Tableau dashboards for the sales team.")]
    result = plan(ANALYSIS, draft("Built dashboards for the sales team."), facts, JOB)
    assert [t.requirement_id for t in result.targets] == ["R2"]


def test_negated_and_non_work_facts_do_not_count_more_than_the_page_would():
    work = analysis(req("R1", "required", "3+ years of professional SQL", ["SQL"], True))
    facts = [
        fact("E1", "Never used Tableau; wrote no SQL."),
        fact("E2", "Used SQL in a class project.", kind="academic"),
    ]
    # The draft already shows the class project at related credit; the facts add nothing.
    result = plan(work, draft("Used SQL in a class project."), facts, JOB)
    assert result.targets == []


def test_skills_facts_and_priority_order():
    facts = [
        fact("E1", "Python", entry_ref=None),
        fact("E2", "Built Tableau dashboards for the sales team."),
    ]
    result = plan(ANALYSIS, draft("Wrote SQL queries."), facts, JOB)
    # Mandatory requirements come first, then by rubric category.
    assert [t.requirement_id for t in result.targets] == ["R2", "R3"]
    assert result.targets[1].fact_aliases == ["E1"]


def test_targets_are_capped_per_attempt():
    many = analysis(*[req(f"R{i}", "terminology", f"Tool{i}", [f"Tool{i}"]) for i in range(1, 12)])
    facts = [fact(f"E{i}", f"Used Tool{i} daily.") for i in range(1, 12)]
    assert len(plan(many, draft("Wrote reports."), facts, JOB).targets) == MAX_TARGETS


def test_fact_passages_keep_alias_and_experience_type():
    passages = fact_passages([fact("E7", "Led a study group.", kind="personal")])
    assert (passages[0].where, passages[0].experience_type) == ("E7", "personal")


def test_judge_accepts_only_strict_improvements():
    before_draft = draft("Wrote SQL queries.")
    before = score(ANALYSIS, draft_passages(before_draft), job_text=JOB)

    better = draft("Wrote SQL queries.", "Built Tableau dashboards.")
    verdict, after, improved = judge(ANALYSIS, before, better, JOB)
    assert (verdict, improved) == ("improved", ["R2"])
    assert after.score > before.score

    # Gains Tableau but loses SQL: one requirement declines, so it is rejected.
    traded = draft("Built Tableau dashboards.")
    assert judge(ANALYSIS, before, traded, JOB)[0] == "no_improvement"

    same = draft("Wrote SQL queries.", "Met with stakeholders.")
    assert judge(ANALYSIS, before, same, JOB)[0] == "no_improvement"


def test_judge_rejects_new_repetition():
    before = score(ANALYSIS, draft_passages(draft("Wrote SQL queries.")), job_text=JOB)
    stuffed = draft(
        "Wrote SQL queries.",
        "Built Tableau dashboards.",
        "Shared Tableau views.",
        "Kept Tableau extracts fresh.",
        skills=["Tableau"],
    )
    assert judge(ANALYSIS, before, stuffed, JOB)[0] == "new_repetition"
