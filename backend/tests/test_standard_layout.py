"""The standard layout (ADR 0011): structured entry headers, emphasis, links,
measured typography, and a render -> read-back round trip (fictional data)."""

from __future__ import annotations

import io
import uuid

import pytest
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from app.documents.convert import resolve_backend
from app.documents.extract import (
    HeaderLink,
    classify_heading,
    extract_docx,
    extract_pdf,
    is_dates,
    safe_link,
    split_bold_terms,
)
from app.documents.pdf_inspect import normalize
from app.render.format import FormatPreferences
from app.render.pipeline import _check_phrases, render_draft
from app.render.reconstructed_docx import ExtraEntry
from app.render.standard_docx import EntryDetail, emphasis_segments, render_standard
from app.tailoring.models import DraftBullet, DraftContent, DraftEntry, DraftSkill

HEADER = ["Avery Example", "Springfield, IL | 555-0100 | avery@example.test | LinkedIn"]
LINKS = [HeaderLink("LinkedIn", "https://www.linkedin.com/in/avery-example")]
IDS = [uuid.uuid4() for _ in range(8)]


def _content() -> tuple[DraftContent, dict, dict]:
    entries = [
        DraftEntry(
            ref="E1",
            section="education",
            entry="Bachelor of Science, Information Systems | Lakeside State University",
            date_text="May 2025",
            experience_type="academic",
            bullets=[
                DraftBullet(
                    text="Relevant Coursework: Database Design, Statistics, Systems Analysis",
                    evidence_ids=[IDS[0]],
                )
            ],
        ),
        DraftEntry(
            ref="E2",
            section="experience",
            entry="Data Analyst Intern | Fictional Retail Co.",
            date_text="June 2024 – August 2024",
            experience_type="internship",
            bullets=[
                DraftBullet(
                    text="Built weekly sales dashboards for 3 regional managers.",
                    evidence_ids=[IDS[1]],
                ),
                DraftBullet(
                    text="Reconciled inventory extracts with SQL every Monday.",
                    evidence_ids=[IDS[2]],
                ),
            ],
        ),
        DraftEntry(
            ref="E3",
            section="projects",
            entry="Store Traffic Forecast",
            date_text="March 2025",
            experience_type="personal",
            bullets=[
                DraftBullet(
                    text="Cleaned 2,400 visit records using Python, Pandas, and NumPy.",
                    evidence_ids=[IDS[3]],
                ),
                DraftBullet(
                    text="Trained a Random Forest model on weekly traffic.", evidence_ids=[IDS[4]]
                ),
            ],
        ),
    ]
    content = DraftContent(
        headline="Data Analyst",
        entries=entries,
        skills=[
            DraftSkill(text="Programming: Python, SQL, Excel", evidence_ids=[IDS[5]]),
            DraftSkill(text="Languages: English, Spanish", evidence_ids=[IDS[6]]),
        ],
        omitted=[],
    )
    details = {
        ("education", entries[0].entry): EntryDetail(
            "Lakeside State University",
            "Springfield, IL",
            "Bachelor of Science, Information Systems",
        ),
        ("experience", entries[1].entry): EntryDetail(
            "Fictional Retail Co.", "Chicago, IL", "Data Analyst Intern"
        ),
    }
    bold = {IDS[3]: ["Python", "Pandas", "NumPy"], IDS[4]: ["Random Forest"]}
    return content, details, bold


def _render() -> bytes:
    content, details, bold = _content()
    return render_standard(
        header_lines=HEADER,
        header_links=LINKS,
        content=content,
        prefs=FormatPreferences(),
        extras=[],
        details=details,
        bold_terms=bold,
    )


# --- Reading resumes ------------------------------------------------------------------


def test_labeled_skill_lines_are_not_section_headings():
    assert classify_heading("Tools: Excel") is None
    assert classify_heading("Tools & Technologies") == "skills"
    assert classify_heading("LEADERSHIP & EXTRA-CURRICULAR ACTIVITIES") == "leadership"


def test_bold_phrases_split_into_terms():
    assert split_bold_terms(["Python, Pandas, and NumPy", "SQL and Python", "Power BI"]) == [
        "Python",
        "Pandas",
        "NumPy",
        "SQL",
        "Power BI",
    ]


def test_only_web_and_mail_links_are_kept():
    assert safe_link("https://www.linkedin.com/in/x") == "https://www.linkedin.com/in/x"
    assert safe_link("mailto:a@example.test") == "mailto:a@example.test"
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "https://", None, "x" * 600):
        assert safe_link(bad) is None


def test_dates_are_recognized_as_a_right_hand_segment():
    assert is_dates("July 2026 – August 2026") and is_dates("May 2026")
    assert is_dates("January 2026 - Present")
    assert not is_dates("Jersey City, NJ")


def test_word_round_trip_recovers_two_line_headers_emphasis_and_links():
    result = extract_docx(_render())
    assert result.header_lines == HEADER
    assert result.header_links == LINKS
    by_text = {c.text: c for c in result.candidates}
    coursework = by_text["Relevant Coursework: Database Design, Statistics, Systems Analysis"]
    assert (
        coursework.organization,
        coursework.location,
        coursework.title,
        coursework.date_text,
    ) == (
        "Lakeside State University",
        "Springfield, IL",
        "Bachelor of Science, Information Systems",
        "May 2025",
    )
    sales = by_text["Built weekly sales dashboards for 3 regional managers."]
    assert sales.entry == "Data Analyst Intern | Fictional Retail Co."
    assert (sales.organization, sales.location, sales.date_text) == (
        "Fictional Retail Co.",
        "Chicago, IL",
        "June 2024 – August 2024",
    )
    cleaned = by_text["Cleaned 2,400 visit records using Python, Pandas, and NumPy."]
    assert cleaned.entry == "Store Traffic Forecast" and cleaned.organization is None
    assert cleaned.bold_terms == ["Python", "Pandas", "NumPy"]
    assert by_text["Programming: Python, SQL, Excel"].section == "skills"


def test_single_line_entries_are_not_merged_with_the_next_entry():
    document = Document()
    document.add_paragraph("Experience")
    for entry, bullet in (
        ("Analyst | Co. A	2024", "Did a thing."),
        ("Intern | Co. B	2023", "Did more."),
    ):
        document.add_paragraph(entry)
        document.add_paragraph(bullet, style="List Bullet")
    buffer = io.BytesIO()
    document.save(buffer)
    entries = [(c.entry, c.organization) for c in extract_docx(buffer.getvalue()).candidates]
    assert entries == [("Analyst | Co. A", None), ("Intern | Co. B", None)]


# --- Rendering ---------------------------------------------------------------------------


def test_emphasis_joins_listed_terms_into_one_bold_run():
    segments = emphasis_segments(
        "using Python, Pandas, and NumPy daily", ["Python", "Pandas", "NumPy"]
    )
    assert [(s.text, s.bold) for s in segments] == [
        ("using ", False),
        ("Python, Pandas, and NumPy", True),
        (" daily", False),
    ]


def test_layout_uses_the_measured_typography():
    document = Document(io.BytesIO(_render()))
    section = document.sections[0]
    # Word stores margins in twentieths of a point: equal to within 0.01 pt.
    assert abs(section.left_margin - Cm(1.6)) < 200 and abs(section.top_margin - Cm(1.4)) < 200
    paragraphs = document.paragraphs
    name = paragraphs[0].runs[0]
    assert name.font.name == "Times New Roman" and name.font.size == Pt(17.5) and name.bold
    heading = next(p for p in paragraphs if p.text == "EDUCATION")
    assert heading.runs[0].font.size == Pt(11.5)
    border = heading._p.pPr.find(qn("w:pBdr")).find(qn("w:bottom"))
    assert border.get(qn("w:sz")) == "8"  # 1 pt rule
    org = next(p for p in paragraphs if p.text.startswith("Fictional Retail Co."))
    assert org.text == "Fictional Retail Co.\tChicago, IL" and org.runs[0].bold
    title = next(p for p in paragraphs if p.text.startswith("Data Analyst Intern"))
    assert title.text == "Data Analyst Intern\tJune 2024 – August 2024" and not title.runs[0].bold
    degree = next(p for p in paragraphs if p.text.startswith("Bachelor of Science"))
    assert degree.runs[0].bold  # education titles are bold, job titles are not
    bullet = next(p for p in paragraphs if p.text.startswith("Cleaned 2,400"))
    assert [r.text for r in bullet.runs if r.bold] == ["Python, Pandas, and NumPy"]
    levels = document.part.numbering_part.element.iter(qn("w:lvlText"))
    assert "•" in {level.get(qn("w:val")) for level in levels}
    links = [r for r in document.part.rels.values() if r.reltype.endswith("/hyperlink")]
    assert [r.target_ref for r in links] == ["https://www.linkedin.com/in/avery-example"]


def test_reading_order_check_searches_forward_for_repeated_phrases():
    text = normalize("Org A Chicago, IL May 2025 Org B Chicago, IL May 2025 top-\nperforming")
    missing, out_of_order = _check_phrases(
        text, [["Org A", "Chicago, IL", "May 2025"], ["Org B", "Chicago, IL", "May 2025"]]
    )
    assert missing == [] and out_of_order == []
    assert "top-performing" in text


@pytest.mark.skipif(resolve_backend() is None, reason="PDF converter not available")
def test_pdf_round_trip_fits_one_page_and_reads_back(tmp_path):
    content, details, bold = _content()
    outcome = render_draft(
        master=None,
        master_kind="pdf",
        header_lines=HEADER,
        content=content,
        facts={},
        prefs=FormatPreferences(),
        extras=[ExtraEntry("education", content.entries[0].entry, "May 2025")],
        header_links=LINKS,
        details=details,
        bold_terms=bold,
    )
    checks = outcome.checks
    assert checks.passed and checks.layout == "standard" and checks.page_count == 1, checks
    assert {"LiberationSerif", "LiberationSerif-Bold"} <= set(checks.fonts)
    result = extract_pdf(outcome.pdf)
    assert result.header_links == LINKS
    by_text = {c.text: c for c in result.candidates}
    sales = by_text["Built weekly sales dashboards for 3 regional managers."]
    assert (sales.organization, sales.location, sales.title) == (
        "Fictional Retail Co.",
        "Chicago, IL",
        "Data Analyst Intern",
    )
    assert by_text["Trained a Random Forest model on weekly traffic."].bold_terms == [
        "Random Forest"
    ]
