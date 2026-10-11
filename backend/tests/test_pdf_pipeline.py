"""End-to-end render checks. Skipped when no converter backend is available."""

import shutil
import subprocess
from pathlib import Path

import pytest
from docx import Document

from app.documents import convert
from app.documents.convert import ConversionError, docx_to_pdf, resolve_backend
from app.documents.docx_edit import TextSegment, edit_copy, list_paragraphs
from app.documents.fixtures import build_fixture_master
from app.documents.pdf_inspect import inspect_pdf

needs_converter = pytest.mark.skipif(resolve_backend() is None, reason="no converter available")
needs_docker = pytest.mark.skipif(
    resolve_backend() != "docker", reason="Docker converter backend not in use"
)


@needs_converter
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
            "Jun 2025 - Aug 2025",
            new_text,
            "Languages: English (native)",
        ],
    )
    assert report.passes(max_pages=1), report
    assert target.text not in report.text
    assert report.page_size_pt == (612.0, 792.0)


@needs_docker
def test_docker_timeout_fails_cleanly_and_leaves_no_container(tmp_path: Path):
    master = build_fixture_master(tmp_path / "master.docx")
    with pytest.raises(ConversionError):
        docx_to_pdf(master, tmp_path / "out.pdf", timeout_seconds=0.5)
    assert not (tmp_path / "out.pdf").exists()
    running = subprocess.run(  # noqa: S603 - fixed arguments
        [shutil.which("docker"), "ps", "-a", "-q", "--filter", "name=convert-"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert running.stdout.strip() == ""


def test_oversized_docx_is_rejected_before_conversion(tmp_path: Path, monkeypatch):
    big = tmp_path / "big.docx"
    with big.open("wb") as handle:
        handle.truncate(convert.MAX_DOCX_BYTES + 1)
    monkeypatch.setattr(convert, "resolve_backend", lambda: pytest.fail("must not run"))
    with pytest.raises(ConversionError, match="larger than"):
        docx_to_pdf(big, tmp_path / "out.pdf")
