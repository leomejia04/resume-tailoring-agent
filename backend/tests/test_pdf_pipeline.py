"""End-to-end render checks. Skipped when LibreOffice is not installed."""

from pathlib import Path

import pytest
from docx import Document

from app.documents.convert import docx_to_pdf, find_soffice
from app.documents.docx_edit import TextSegment, edit_copy, list_paragraphs
from app.documents.fixtures import build_fixture_master
from app.documents.pdf_inspect import inspect_pdf

pytestmark = pytest.mark.skipif(find_soffice() is None, reason="LibreOffice not installed")


def test_edited_master_renders_to_one_clean_page(tmp_path: Path):
    master = build_fixture_master(tmp_path / "master.docx")
    target = next(r for r in list_paragraphs(Document(str(master))) if r.is_bullet)
    edited = tmp_path / "edited.docx"
    new_text = "Automated weekly sales reconciliation with Python and SQL across three regions."
    edit_copy(master, edited, target.index, target.text, [TextSegment(new_text)])

    report = inspect_pdf(
        docx_to_pdf(edited, tmp_path / "edited.pdf"),
        expected_phrases=[
            "Jordan Example",
            "jordan@example.com",
            "May 2025",
            new_text,
            "Jun 2025 - Aug 2025",
            "Languages: English (native)",
        ],
    )
    assert report.passes(max_pages=1), report
    assert target.text not in report.text
    assert report.page_size_pt == (612.0, 792.0)
