"""PDF inspection tests using small hand-built PDFs (no LibreOffice needed)."""

from pathlib import Path

from app.documents.pdf_inspect import inspect_pdf, render_page_png

LETTER = (612, 792)


def write_pdf(path: Path, pages: list[list[tuple[float, float, str]]]) -> Path:
    """Write a minimal valid PDF; each page is a list of (x, y, text) in points.

    PDF y coordinates start at the bottom of the page. Text uses the standard
    Helvetica font, so nothing needs embedding.
    """
    objects: list[bytes] = []
    page_ids = [3 + 2 * i for i in range(len(pages))]
    font_id = 3 + 2 * len(pages)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for pid, lines in zip(page_ids, pages, strict=True):
        ops = "".join(f"BT /F1 10 Tf {x} {y} Td ({text}) Tj ET\n" for x, y, text in lines)
        stream = ops.encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {LETTER[0]} {LETTER[1]}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {pid + 1} 0 R >>".encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def test_clean_page_passes(tmp_path):
    pdf = write_pdf(
        tmp_path / "ok.pdf",
        [[(50, 740, "Jordan Example"), (50, 720, "jordan@example.com"), (50, 700, "Skills")]],
    )
    report = inspect_pdf(pdf, ["Jordan Example", "jordan@example.com", "Skills"])
    assert report.passes(max_pages=1), report
    assert report.page_count == 1
    assert report.page_size_pt == (612.0, 792.0)
    assert report.fonts == ["Helvetica"]


def test_detects_missing_and_out_of_order_phrases(tmp_path):
    pdf = write_pdf(tmp_path / "order.pdf", [[(50, 740, "Experience"), (50, 720, "Education")]])
    report = inspect_pdf(pdf, ["Education", "Experience", "Projects"])
    assert report.missing_phrases == ["Projects"]
    assert report.out_of_order_phrases == ["Experience"]
    assert not report.passes()


def test_detects_text_running_off_the_page(tmp_path):
    pdf = write_pdf(tmp_path / "clip.pdf", [[(50, 740, "Fine"), (580, 400, "Overflowing")]])
    report = inspect_pdf(pdf)
    assert any("Overflowing" in span for span in report.clipped_spans)
    assert not report.passes()


def test_two_pages_fail_one_page_limit(tmp_path):
    pdf = write_pdf(tmp_path / "two.pdf", [[(50, 740, "Page one")], [(50, 740, "Page two")]])
    report = inspect_pdf(pdf)
    assert report.page_count == 2
    assert not report.passes(max_pages=1)
    assert report.passes(max_pages=2)


def test_image_only_pdf_is_not_searchable(tmp_path):
    report = inspect_pdf(write_pdf(tmp_path / "blank.pdf", [[]]))
    assert not report.searchable
    assert not report.passes()


def test_render_page_png(tmp_path):
    pdf = write_pdf(tmp_path / "ok.pdf", [[(50, 740, "Jordan Example")]])
    png = render_page_png(pdf, tmp_path / "page.png", dpi=72)
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
