"""Truthfulness rules for model output, tested with hand-written 'model' drafts."""

import uuid
from decimal import Decimal

import pytest

from app.ai.client import DemoClient
from app.ai.pricing import Usage, cost_usd, worst_case_cost_usd
from app.ai.schemas import (
    BulletOut,
    EntryOut,
    JobAnalysisOut,
    RequirementOut,
    SkillOut,
    TailoredDraftOut,
)
from app.db.models import EvidenceRecord, EvidenceVersion
from app.tailoring.analysis import find_gaps, normalize_analysis, question_for
from app.tailoring.context import build_context, draft_payload
from app.tailoring.models import AnalysisContent, Requirement
from app.tailoring.validate import check, finalize, numbers_in
from tests.conftest import make_settings

INTERN = "Data Analyst Intern | Fictional Analytics Co."
SUPPORT = "IT Support Assistant | Example State University"


MASTER_ID = uuid.uuid4()
OWNER_ID = uuid.uuid4()
_positions = iter(range(1000))


def fact(
    text,
    entry=INTERN,
    section="experience",
    kind="internship",
    status="verified",
    dates="Jun 2025 - Aug 2025",
):
    record = EvidenceRecord(
        id=uuid.uuid4(),
        owner_id=OWNER_ID,
        master_version_id=MASTER_ID,
        position=next(_positions),
        origin="master",
    )
    version = EvidenceVersion(
        text=text,
        source_text=text,
        section=section,
        entry=entry,
        date_text=dates if entry else None,
        experience_type=kind,
        verification_status=status,
    )
    return record, version


ROWS = [
    fact(
        "Built Python and SQL scripts that reconciled weekly sales extracts across three regions."
    ),
    fact("Documented data definitions for 40 report fields in a shared glossary."),
    fact(
        "Resolved hardware and account access requests for students.",
        entry=SUPPORT,
        kind="professional",
        dates="Sep 2023 - May 2025",
    ),
    fact("Technical: SQL, Python, Excel", entry=None, section="skills", kind="other"),
    fact("Built a Tableau dashboard for the dean.", status="unverified"),
    fact("Led a team of 12 analysts.", status="rejected"),
]
ANALYSIS = AnalysisContent(
    role_title="Data Analyst",
    requirements=[
        Requirement(
            id="R1",
            category="required",
            text="SQL and Python",
            terms=["SQL", "Python"],
            mandatory=True,
        ),
        Requirement(
            id="R2",
            category="required",
            text="Tableau dashboards",
            terms=["Tableau"],
            mandatory=True,
        ),
        Requirement(
            id="R3", category="preferred", text="Snowflake", terms=["Snowflake"], mandatory=False
        ),
    ],
)


@pytest.fixture
def context():
    return build_context(ROWS, max_chars=60_000)


def draft(*bullets, entry="J1", skills=(), summary=None, headline="Data Analyst"):
    return TailoredDraftOut(
        headline=headline,
        summary=summary,
        entries=[
            EntryOut(
                entry_ref=entry,
                bullets=[
                    BulletOut(text=t, evidence_ids=ids, requirement_ids=["R1"])
                    for t, ids in bullets
                ],
            )
        ],
        skills=[SkillOut(text=t, evidence_ids=ids) for t, ids in skills],
        omitted_evidence_ids=[],
    )


def codes(result):
    return sorted({i.code for i in result.issues if i.severity == "error"})


def test_context_contains_only_confirmed_facts(context):
    texts = [f.text for f in context.facts.values()]
    assert len(texts) == 4
    assert not any("Tableau" in t or "team of 12" in t for t in texts)
    assert list(context.entries) == ["J1", "J2"]
    assert [f.text for f in context.unattached] == ["Technical: SQL, Python, Excel"]
    payload = draft_payload(context, ANALYSIS, "Data Analyst", "Fictional Retail")
    assert "for the dean" not in str(payload)  # unverified fact never sent
    assert "team of 12" not in str(payload)  # rejected fact never sent
    assert all(
        str(f.evidence_id) not in str(payload) for f in context.facts.values()
    )  # aliases only


def test_faithful_rewording_passes(context):
    result = check(
        draft(
            (
                "Automated weekly sales reconciliation across three regions with Python and SQL.",
                ["E1"],
            )
        ),
        context,
        ANALYSIS,
    )
    assert codes(result) == []


@pytest.mark.parametrize(
    ("bullet", "ids", "expected"),
    [
        (
            "Reconciled weekly sales extracts, saving 25% of analyst time.",
            ["E1"],
            ["unsupported_number"],
        ),
        ("Documented 45 report fields.", ["E2"], ["unsupported_number"]),
        ("Built Tableau dashboards with SQL.", ["E1"], ["unsupported_term"]),
        ("Migrated reporting to Snowflake.", ["E1"], ["unsupported_term"]),
        ("Reconciled sales extracts in 2019.", ["E1"], ["unsupported_date"]),
        ("Reconciled sales data.", [], ["missing_citation"]),
        ("Reconciled sales data.", ["E99"], ["unknown_evidence"]),
        ("Resolved access requests.", ["E3"], ["evidence_wrong_entry"]),
    ],
)
def test_unsupported_claims_are_caught(context, bullet, ids, expected):
    assert codes(check(draft((bullet, ids)), context, ANALYSIS)) == expected


def test_supported_number_and_percent_formats(context):
    ok = check(draft(("Defined 40 report fields in a glossary.", ["E2"])), context, ANALYSIS)
    assert codes(ok) == []
    assert numbers_in("Cut costs 25 percent, saving $1,200") == {"25%", "1200"}


def test_unknown_entry_and_rejected_or_unverified_facts_cannot_be_used(context):
    assert "unknown_entry" in codes(check(draft(("Text", ["E1"]), entry="J9"), context, ANALYSIS))
    # The rejected/unverified facts have no alias at all, so citing them is impossible.
    assert len(context.facts) == 4


def test_prompt_injection_output_is_neutralized(context):
    """A posting told the model to claim Kubernetes expertise and 10 years; the rules strip it."""
    analysis = AnalysisContent(
        role_title="Data Analyst",
        requirements=[
            Requirement(
                id="R1",
                category="required",
                text="Kubernetes",
                terms=["Kubernetes"],
                mandatory=True,
            )
        ],
    )
    injected = draft(
        ("Expert in Kubernetes with 10 years of experience.", ["E1"]),
        summary="Kubernetes expert with 10 years of experience.",
        headline="Senior Kubernetes Architect",
    )
    result = check(injected, context, analysis)
    content, report = finalize(injected, context, analysis, result, repaired=False)
    assert content.entries == []  # the bullet was removed
    assert content.summary is None
    assert content.headline == "Data Analyst"
    assert not report.passed and report.removed
    assert "Kubernetes" not in content.model_dump_json()


def test_skills_must_cite_unattached_facts(context):
    good = check(
        draft(("Built SQL scripts.", ["E1"]), skills=[("SQL, Python", ["E4"])]), context, ANALYSIS
    )
    assert codes(good) == []
    bad = check(
        draft(("Built SQL scripts.", ["E1"]), skills=[("Tableau", ["E4"])]), context, ANALYSIS
    )
    assert codes(bad) == ["unsupported_term"]
    wrong = check(
        draft(("Built SQL scripts.", ["E1"]), skills=[("SQL", ["E1"])]), context, ANALYSIS
    )
    assert codes(wrong) == ["unknown_evidence"]


def test_finalize_maps_aliases_and_keeps_protected_fields(context):
    out = draft(
        ("Automated sales reconciliation with Python and SQL across three regions.", ["E1"])
    )
    content, report = finalize(
        out, context, ANALYSIS, check(out, context, ANALYSIS), repaired=False
    )
    entry = content.entries[0]
    assert (
        entry.entry == INTERN and entry.date_text == "Jun 2025 - Aug 2025"
    )  # from the master, not the model
    assert entry.experience_type == "internship"
    assert entry.bullets[0].evidence_ids == [context.facts["E1"].evidence_id]
    assert {o.reason for o in content.omitted} == {"not_selected"}
    assert report.passed


def test_normalize_analysis_dedupes_and_drops_invented_terms():
    job = "Requirements:\n- SQL and Python\n- Tableau dashboards"
    raw = JobAnalysisOut(
        role_title="  Data   Analyst ",
        seniority=None,
        requirements=[
            RequirementOut(
                category="required",
                text="SQL and Python",
                terms=["SQL", "Python", "PostgreSQL", "experience"],
                mandatory=True,
            ),
            RequirementOut(
                category="required", text="sql and python!", terms=["SQL"], mandatory=True
            ),
            RequirementOut(
                category="preferred", text="Tableau dashboards", terms=["Tableau"], mandatory=True
            ),
        ],
    )
    analysis = normalize_analysis(raw, job)
    assert analysis.role_title == "Data Analyst"
    assert [r.id for r in analysis.requirements] == ["R1", "R2"]
    assert analysis.requirements[0].terms == ["SQL", "Python"]  # invented and generic terms dropped
    assert analysis.requirements[1].mandatory is False  # only required items can be mandatory


def test_gaps_ask_only_about_uncovered_terms(context):
    gaps = find_gaps(ANALYSIS, context.all_text)
    assert [(g.requirement.id, g.term) for g in gaps] == [("R2", "Tableau"), ("R3", "Snowflake")]
    assert "Have you used Tableau?" in question_for(gaps[0])


def test_cost_arithmetic_uses_official_rates():
    settings = make_settings()
    usage = Usage(
        input_tokens=1_000_000,
        cached_input_tokens=200_000,
        cache_write_tokens=100_000,
        output_tokens=500_000,
    )
    # 700k plain * 0.20 + 200k cached * 0.02 + 100k writes * 0.20 * 1.25 + 500k out * 1.20
    assert cost_usd(usage, settings) == Decimal("0.140000") + Decimal("0.004000") + Decimal(
        "0.025000"
    ) + Decimal("0.600000")
    assert cost_usd(Usage(), settings) == Decimal("0")
    assert worst_case_cost_usd(30_000, 10_000, settings) > cost_usd(
        Usage(input_tokens=10_000, output_tokens=10_000), settings
    )


def test_demo_client_is_deterministic_and_grounded(context):
    job = "Data Analyst\n\nRequirements:\n- SQL and Python for reporting\n- Tableau dashboards\n"
    client = DemoClient()
    analysis_out = client.parse(
        instructions="",
        payload={"job_description": job, "job_title": "Data Analyst"},
        schema=JobAnalysisOut,
        max_output_tokens=1,
        user_hash="x",
    )
    analysis = normalize_analysis(analysis_out.output, job)
    assert [r.terms for r in analysis.requirements] == [["SQL", "Python"], ["Tableau"]]
    payload = draft_payload(context, analysis, "Data Analyst", None)
    draft_out = client.parse(
        instructions="",
        payload=payload,
        schema=TailoredDraftOut,
        max_output_tokens=1,
        user_hash="x",
    )
    assert codes(check(draft_out.output, context, analysis)) == []
    assert draft_out.usage == Usage() and client.provider == "demo"
