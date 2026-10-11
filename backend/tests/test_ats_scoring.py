"""ATS match estimate: matching rules, rubric arithmetic, passages, and comparisons.

Pure functions only; every resume line and posting here is fictional.
"""

import uuid
from fractions import Fraction

import pytest

from app.ats.matching import clean, content_stems, find_term
from app.ats.passages import Passage, draft_passages, pdf_passages, text_passages
from app.ats.scoring import RUBRIC_VERSION, WEIGHTS, compare, score
from app.render.format import FormatPreferences
from app.tailoring.models import (
    AnalysisContent,
    DraftBullet,
    DraftContent,
    DraftEntry,
    DraftSkill,
    Requirement,
)


def req(rid, category, text, terms, mandatory=False):
    return Requirement(id=rid, category=category, text=text, terms=terms, mandatory=mandatory)


def analysis(*requirements):
    return AnalysisContent(role_title="Data Analyst", requirements=list(requirements))


def lines(*texts, kind=None):
    return [Passage(f"line {i}", clean(t), experience_type=kind) for i, t in enumerate(texts, 1)]


def bullet(text, links=()):
    return DraftBullet(text=text, evidence_ids=[uuid.uuid4()], requirement_ids=list(links))


def entry(ref, title, bullets, kind="professional", section="experience"):
    return DraftEntry(
        ref=ref,
        section=section,
        entry=title,
        date_text="Jan 2024 - Present",
        experience_type=kind,
        bullets=bullets,
    )


def draft(entries, skills=(), show_headline=False, summary=None):
    return DraftContent(
        headline="Data Analyst",
        summary=summary,
        entries=entries,
        skills=[DraftSkill(text=s, evidence_ids=[uuid.uuid4()]) for s in skills],
        omitted=[],
        format=FormatPreferences(show_headline=show_headline),
    )


def by_id(result, rid):
    return next(r for r in result.requirements if r.id == rid)


# --- Matching -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "term", "found"),
    [
        ("Used R and Python", "R", ["R"]),
        ("Led R&D reporting", "R", []),
        ("Wrote C code", "C", ["C"]),
        ("Built C++ and C# services", "C", []),
        ("Briefed the C-suite", "C", []),
        ("Shaped the go-to-market plan", "Go", []),
        ("Go-to-market plan", "Go", []),
        ("Built JavaScript apps", "Java", []),
        ("Moved data to NoSQL stores", "SQL", []),
        ("Built Power-BI and PowerBI dashboards", "Power BI", ["Power-BI", "PowerBI"]),
        ("Dashboard design", "dashboards", ["Dashboard"]),
        ("Excel-based models", "Excel", ["Excel"]),
        ("Learned to excel at teamwork", "Excel", []),
        ("Microsoft Teams rollout", "Teams", ["Teams"]),
        ("Team lead for interns", "Teams", []),
        ("Built ﬁnance dashboards", "finance", ["finance"]),  # ligature from PDF text
    ],
)
def test_terms_match_whole_words_only(text, term, found):
    cleaned = clean(text)
    assert [cleaned[h.start : h.end] for h in find_term(cleaned, term)] == found


@pytest.mark.parametrize(
    ("text", "term", "negated"),
    [
        ("No prior experience with SQL", "SQL", True),
        ("Didn't use Tableau", "Tableau", True),
        ("Migrated non-SQL stores", "SQL", True),
        ("SQL experience is not required", "SQL", True),
        ("Python, not R", "R", True),
        ("Not only SQL but also Python", "SQL", False),
        ("Reduced errors without manual steps using Python", "Python", False),
        ("No downtime. Wrote SQL reports", "SQL", False),  # negation ends at the sentence
    ],
)
def test_negation_is_clause_scoped(text, term, negated):
    hits = find_term(clean(text), term)
    assert hits and all(h.negated == negated for h in hits)


def test_stems_join_word_forms():
    assert content_stems("processes processing process") == {"process"}
    assert content_stems("analyzed analyze") == {"analyz"}
    assert "experience" not in content_stems("strong experience")  # posting filler


# --- Rubric arithmetic ------------------------------------------------------------


def test_weights_are_documented_and_sum_to_one():
    assert {
        "required": Fraction(1, 2),
        "responsibility": Fraction(1, 4),
        "preferred": Fraction(3, 20),
        "terminology": Fraction(1, 10),
    } == WEIGHTS
    assert sum(WEIGHTS.values()) == 1


def test_overall_score_is_the_weighted_mean_of_categories():
    a = analysis(
        req("R1", "required", "SQL", ["SQL"]),
        req("R2", "responsibility", "Build Tableau reports", ["Tableau"]),
        req("R3", "preferred", "Python scripting", ["Python"]),
        req("R4", "terminology", "KPI", ["KPI"]),
    )
    result = score(a, lines("Wrote SQL queries tracking each KPI"))
    assert result.score == 60  # 50 (required) + 10 (terminology)
    assert result.rubric_version == RUBRIC_VERSION
    assert {c.category: c.score for c in result.categories} == {
        "required": 100.0,
        "responsibility": 0.0,
        "preferred": 0.0,
        "terminology": 100.0,
    }


def test_absent_categories_are_renormalized():
    a = analysis(
        req("R1", "required", "SQL", ["SQL"]),
        req("R2", "preferred", "Python", ["Python"]),
    )
    result = score(a, lines("SQL reporting"))
    # required weight 50/65, preferred 15/65: 100 * 50/65 = 76.9 -> 77
    assert result.score == 77
    assert [c.weight for c in result.categories] == [round(50 / 65, 4), round(15 / 65, 4)]


def test_partial_credit_is_the_share_of_terms_but_at_least_related():
    a = analysis(
        req("R1", "required", "SQL and Python", ["SQL", "Python"]),
        req("R2", "required", "SQL, Python, and Tableau", ["SQL", "Python", "Tableau"]),
        req("R3", "required", "SQL, Python, Tableau, Excel", ["SQL", "Python", "Tableau", "Excel"]),
    )
    result = score(a, lines("Automated SQL and Python reports"))
    assert by_id(result, "R1").credit == 1.0
    assert by_id(result, "R2").credit == round(2 / 3, 4)
    assert by_id(result, "R3").credit == 0.5
    assert by_id(result, "R3").missing_terms == ["Tableau", "Excel"]
    assert "Found 2 of 4 terms." in by_id(result, "R3").notes


def test_repeating_a_keyword_adds_nothing():
    a = analysis(req("R1", "required", "SQL", ["SQL"]), req("R2", "required", "Python", ["Python"]))
    once = score(a, lines("Wrote SQL queries"))
    stuffed = score(a, lines("SQL SQL SQL", "SQL queries in SQL", "More SQL"))
    assert once.score == stuffed.score == 50
    assert [(t.term, t.mentions) for t in stuffed.repeated_terms] == [("SQL", 6)]
    assert once.repeated_terms == []


def test_duplicate_requirements_are_merged_into_the_most_important_category():
    a = analysis(
        req("R1", "terminology", "SQL", ["sql"]),
        req("R2", "required", "Advanced SQL", ["SQL"], mandatory=True),
        req("R3", "preferred", "Python", ["Python"]),
    )
    result = score(a, lines("Python scripts"))
    assert [r.id for r in result.requirements] == ["R2", "R3"]
    assert by_id(result, "R2").merged_ids == ["R1"]
    assert result.mandatory_gaps == ["R2"]
    assert {c.category for c in result.categories} == {"required", "preferred"}


def test_substring_matches_do_not_count():
    a = analysis(req("R1", "required", "Java development", ["Java"]))
    result = score(a, lines("Built JavaScript dashboards"))
    assert by_id(result, "R1").status == "missing"
    assert result.score == 0


def test_negated_resume_mentions_are_uncertain_not_credited():
    a = analysis(req("R1", "required", "SQL", ["SQL"]))
    result = score(a, lines("Reconciled accounts with no SQL tooling"))
    r1 = by_id(result, "R1")
    assert (r1.status, r1.credit) == ("uncertain", 0.0)
    assert r1.matches[0].kind == "negated"
    assert result.mandatory_gaps == []


def test_terms_the_posting_negates_are_not_scored():
    job = "Data analyst. SQL required. No Java experience required."
    a = analysis(req("R1", "required", "SQL", ["SQL"]), req("R2", "preferred", "Java", ["Java"]))
    result = score(a, lines("SQL reporting"), job_text=job)
    r2 = by_id(result, "R2")
    assert (r2.scored, r2.excluded_terms) == (False, ["Java"])
    assert result.score == 100  # only the scored requirement counts
    assert [c.category for c in result.categories] == ["required"]


def test_professional_requirements_cap_academic_only_evidence():
    a = analysis(req("R1", "required", "3+ years of professional SQL experience", ["SQL"]))
    academic = score(a, lines("Course project in SQL", kind="academic"))
    internship = score(a, lines("Built SQL reports", kind="internship"))
    assert (by_id(academic, "R1").status, by_id(academic, "R1").credit) == ("related", 0.5)
    assert (by_id(internship, "R1").status, by_id(internship, "R1").credit) == ("direct", 1.0)


def test_draft_links_need_shared_wording():
    a = analysis(req("R1", "responsibility", "Partner with stakeholders on reporting needs", []))
    linked = [Passage("b1", "Met weekly with stakeholders to scope reports", frozenset({"R1"}))]
    unrelated = [Passage("b1", "Organized the office move", frozenset({"R1"}))]
    good = by_id(score(a, linked), "R1")
    bad = by_id(score(a, unrelated), "R1")
    assert (good.status, good.credit, good.matches[0].basis) == ("related", 0.5, "draft_link")
    assert (bad.status, bad.credit) == ("uncertain", 0.0)
    assert "share no wording" in bad.notes[-1]


def test_strong_shared_wording_counts_as_related_without_a_link():
    a = analysis(req("R1", "responsibility", "Build dashboards to track sales performance", []))
    strong = by_id(score(a, lines("Built sales dashboards for managers")), "R1")
    weak = by_id(score(a, lines("Answered sales calls")), "R1")
    assert (strong.status, strong.matches[0].basis) == ("related", "shared_wording")
    assert (weak.status, weak.credit) == ("uncertain", 0.0)
    assert "No specific terms" in weak.notes[0]


def test_mandatory_gaps_and_partials_are_listed_separately():
    a = analysis(
        req("R1", "required", "SQL", ["SQL"], mandatory=True),
        req("R2", "required", "Python and R", ["Python", "R"], mandatory=True),
        req("R3", "required", "Tableau", ["Tableau"]),
    )
    result = score(a, lines("Python automation"))
    assert result.mandatory_gaps == ["R1"]
    assert result.mandatory_partial == ["R2"]
    assert result.counts == {"direct": 1, "related": 0, "missing": 2, "uncertain": 0}


def test_nothing_to_score_has_no_number():
    result = score(analysis(), lines("Anything"))
    assert result.score is None and result.categories == []


# --- Passages -------------------------------------------------------------------


def sample_draft(show_headline=False):
    return draft(
        [
            entry(
                "E1",
                "Data Analyst Intern | Fictional Analytics Co.",
                [
                    bullet("Built Tableau dashboards for weekly sales reviews", ["R2"]),
                    bullet("Wrote SQL queries to reconcile order data", ["R1"]),
                ],
                kind="internship",
            ),
            entry(
                "E2",
                "B.S. Information Systems | Example State University",
                [bullet("B.S. Information Systems | Example State University", ["R3"])],
                kind="academic",
                section="education",
            ),
        ],
        skills=["Python, Excel"],
        show_headline=show_headline,
    )


def test_draft_passages_follow_what_is_shown():
    hidden = draft_passages(sample_draft())
    shown = draft_passages(sample_draft(show_headline=True))
    assert "Headline" not in [p.where for p in hidden]
    assert shown[0].where == "Headline"
    degree = next(p for p in hidden if p.text.startswith("B.S."))
    assert (degree.requirement_ids, degree.experience_type) == (frozenset({"R3"}), "academic")
    assert sum(p.text.startswith("B.S.") for p in hidden) == 1  # entry-line bullet folded in
    assert next(p for p in hidden if "SQL" in p.text).experience_type == "internship"


def test_pdf_passages_score_only_what_the_pdf_contains():
    content = sample_draft()
    pdf_text = (
        "Pat Example\npat@example.com\nEXPERIENCE\n"
        "Data Analyst Intern | Fictional Analytics Co. Jan 2024 - Present\n"
        "• Wrote SQL queries to reconcile\norder data\n"  # wrapped line; Tableau bullet dropped
        "EDUCATION\nB.S. Information Systems | Example State University\nSKILLS\nPython, Excel\n"
    )
    passages = pdf_passages(content, pdf_text)
    sql = next(p for p in passages if "SQL" in p.text)
    assert (sql.where.endswith("(PDF)"), sql.requirement_ids) == (True, frozenset({"R1"}))
    assert not any("Tableau" in p.text for p in passages)
    assert any(p.where == "PDF text" and "pat@example.com" in p.text for p in passages)

    a = analysis(
        req("R1", "required", "SQL", ["SQL"]), req("R2", "responsibility", "Tableau", ["Tableau"])
    )
    draft_report = score(a, draft_passages(content))
    pdf_report = score(a, passages)
    assert by_id(draft_report, "R2").status == "direct"
    assert by_id(pdf_report, "R2").status == "missing"


def test_text_passages_join_wrapped_lines():
    passages = text_passages("Analyst\n• Built reports for\nfinance teams\n\nSkills: SQL", "Master")
    assert [p.text for p in passages] == [
        "Analyst",
        "• Built reports for finance teams",
        "Skills: SQL",
    ]
    assert passages[1].where == "Master, line 2"


# --- Comparison -----------------------------------------------------------------


def test_compare_requires_the_same_rubric_and_requirements():
    a = analysis(req("R1", "required", "SQL", ["SQL"]), req("R2", "required", "Python", ["Python"]))
    baseline = score(a, lines("Python scripts"))
    tailored = score(a, lines("Python scripts and SQL reports"))
    comparison = compare(baseline, tailored)
    assert (comparison.compatible, comparison.delta, comparison.improved) == (True, 50, ["R1"])

    other = score(analysis(req("R1", "required", "SQL", ["SQL"])), lines("SQL"))
    assert compare(baseline, other).compatible is False
    old_rubric = tailored.model_copy(update={"rubric_version": "ats-rubric/v0"})
    assert "rubric" in (compare(baseline, old_rubric).reason or "")


def test_reports_round_trip_as_validated_json():
    a = analysis(req("R1", "required", "SQL", ["SQL"]))
    result = score(a, lines("SQL"))
    assert type(result).model_validate_json(result.model_dump_json()) == result
