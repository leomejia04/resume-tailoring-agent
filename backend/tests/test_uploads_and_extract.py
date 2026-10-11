import io
import zipfile
from pathlib import Path

import pytest

from app.documents.extract import classify_heading, extract_docx, find_metrics, split_entry
from app.documents.fixtures import build_fixture_master
from app.documents.uploads import (
    DOCX_MIME,
    OLE_SIGNATURE,
    UploadRejectedError,
    safe_label,
    validate_upload,
)
from tests.test_pdf_inspect import write_pdf

LIMIT = 5 * 1024 * 1024


@pytest.fixture(scope="module")
def docx_bytes(tmp_path_factory) -> bytes:
    return build_fixture_master(tmp_path_factory.mktemp("m") / "master.docx").read_bytes()


def rezip(
    data: bytes, extra: dict[str, bytes] | None = None, compress=zipfile.ZIP_DEFLATED
) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", compress) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        for name, payload in (extra or {}).items():
            dst.writestr(name, payload)
    return out.getvalue()


def reject(data: bytes, name: str) -> UploadRejectedError:
    with pytest.raises(UploadRejectedError) as excinfo:
        validate_upload(data, name, LIMIT)
    return excinfo.value


def test_valid_docx(docx_bytes):
    upload = validate_upload(docx_bytes, "C:\\Users\\me\\My Resume (final).docx", LIMIT)
    assert upload.kind == "docx" and upload.content_type == DOCX_MIME
    assert upload.label == "My Resume (final).docx"
    assert len(upload.sha256) == 64


def test_valid_pdf(tmp_path):
    pdf = write_pdf(tmp_path / "r.pdf", [[(50, 740, "EXPERIENCE")]]).read_bytes()
    assert validate_upload(pdf, "resume.pdf", LIMIT).kind == "pdf"


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("../../etc/passwd", "passwd"),
        ("<script>.docx", "script_.docx"),
        ("", "resume"),
        ("...", "resume"),
    ],
)
def test_safe_label(name, label):
    assert safe_label(name) == label


def test_type_comes_from_bytes_not_name(docx_bytes):
    assert reject(docx_bytes, "resume.pdf").code == "unsupported_format"
    assert reject(b"MZ\x90\x00 fake exe", "resume.docx").code == "unsupported_format"
    assert reject(b"%PDF-1.4 but called docx", "resume.docx").code == "unsupported_format"


def test_size_and_empty(docx_bytes):
    assert reject(b"", "r.docx").code == "empty_file"
    with pytest.raises(UploadRejectedError) as excinfo:
        validate_upload(docx_bytes, "r.docx", max_bytes=1000)
    assert excinfo.value.code == "file_too_large"


def test_legacy_or_encrypted_office_files_rejected():
    assert reject(OLE_SIGNATURE + b"\x00" * 600, "r.docx").code == "unsupported_format"


def test_macros_and_embedded_objects_rejected(docx_bytes):
    assert (
        reject(rezip(docx_bytes, {"word/vbaProject.bin": b"x"}), "r.docx").code
        == "unsupported_content"
    )
    assert (
        reject(rezip(docx_bytes, {"word/embeddings/oleObject1.bin": b"x"}), "r.docx").code
        == "unsupported_content"
    )


def test_zip_bomb_rejected(docx_bytes):
    bomb = rezip(docx_bytes, {"word/media/huge.bin": b"\0" * (20 * 1024 * 1024)})
    assert len(bomb) < LIMIT  # small on disk...
    assert reject(bomb, "r.docx").code == "invalid_file"  # ...but expands far too much


def test_path_traversal_entry_rejected(docx_bytes):
    assert reject(rezip(docx_bytes, {"../evil.xml": b"x"}), "r.docx").code == "invalid_file"


def test_not_a_word_package_rejected():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("hello.txt", "hi")
    assert reject(out.getvalue(), "r.docx").code == "invalid_file"


def test_long_pdf_rejected(tmp_path):
    pdf = write_pdf(
        tmp_path / "long.pdf", [[(50, 740, f"Page {i}")] for i in range(11)]
    ).read_bytes()
    assert "at most 10 pages" in reject(pdf, "r.pdf").message


def test_extract_docx_fixture(docx_bytes):
    result = extract_docx(docx_bytes)
    assert result.status == "complete"
    first = result.candidates[0]
    assert (first.section, first.suggested_type, first.date_text) == (
        "education",
        "academic",
        "May 2025",
    )
    intern = result.candidates[1]
    assert intern.suggested_type == "internship" and intern.date_text == "Jun 2025 - Aug 2025"
    assert intern.entry == "Data Analyst Intern | Fictional Analytics Co., Springfield"
    assert any(c.metrics == ["40"] for c in result.candidates)
    # Name and contact lines are identity, not evidence.
    assert not any("jordan@example.com" in c.text for c in result.candidates)


def test_extraction_helpers():
    assert classify_heading("WORK EXPERIENCE") == "experience"
    assert classify_heading("Technical Skills") == "skills"
    assert classify_heading("Built dashboards for the team") is None
    assert split_entry("Analyst | Co.\tJan 2024 \u2013 Present") == (
        "Analyst | Co.",
        "Jan 2024 \u2013 Present",
    )
    assert find_metrics("Cut costs 25% for $1.2M across 3 regions in 2024") == ["25%", "$1.2M", "3"]


def test_pdf_without_text_is_reported(tmp_path):
    from app.documents.extract import extract_pdf

    result = extract_pdf(write_pdf(tmp_path / "blank.pdf", [[]]).read_bytes())
    assert result.status == "failed"
    assert "text recognition" in result.notes[0]


def test_fixture_file_untouched_by_validation(tmp_path):
    path = build_fixture_master(Path(tmp_path) / "m.docx")
    before = path.read_bytes()
    validate_upload(before, "m.docx", LIMIT)
    extract_docx(before)
    assert path.read_bytes() == before
