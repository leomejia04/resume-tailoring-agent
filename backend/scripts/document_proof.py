"""Milestone 1 document proof.

Builds the fictional master, edits one bullet in a copy, verifies formatting,
renders both versions to PDF (when LibreOffice is available), inspects the
PDFs, and writes a JSON report plus page images to ``backend/out/proof``.

Usage (from backend/):  .venv/Scripts/python -m scripts.document_proof
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from docx import Document

from app.documents.convert import ConversionUnavailableError, docx_to_pdf
from app.documents.docx_edit import TextSegment, edit_copy, list_paragraphs, sha256_file
from app.documents.docx_verify import compare_formatting
from app.documents.fixtures import build_fixture_master
from app.documents.pdf_inspect import inspect_pdf, render_page_png

OUT = Path(__file__).resolve().parents[1] / "out" / "proof"
NEW_BULLET = [
    TextSegment("Automated weekly sales reconciliation with "),
    TextSegment("Python", bold=True),
    TextSegment(" and "),
    TextSegment("SQL", bold=True),
    TextSegment(" across three regional extracts."),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    master = build_fixture_master(OUT / "fictional_master.docx")
    master_hash = sha256_file(master)

    target = next(r for r in list_paragraphs(Document(str(master))) if r.is_bullet)
    edited = OUT / "fictional_edited.docx"
    edit_copy(master, edited, target.index, target.text, NEW_BULLET)
    formatting = compare_formatting(master, edited, target.index)
    new_text = "".join(s.text for s in NEW_BULLET)

    result: dict = {
        "target_paragraph": target.index,
        "old_text": target.text,
        "new_text": new_text,
        "master_unchanged": sha256_file(master) == master_hash,
        "formatting_ok": formatting.ok,
        "formatting": asdict(formatting),
    }

    expected = ["Jordan Example", "jordan@example.com", new_text, "Languages: English (native)"]
    try:
        for label, docx_path in (("master", master), ("edited", edited)):
            pdf = docx_to_pdf(docx_path, OUT / f"fictional_{label}.pdf")
            render_page_png(pdf, OUT / f"fictional_{label}.png")
            phrases = expected if label == "edited" else expected[:2]
            report = inspect_pdf(pdf, phrases)
            summary = asdict(report)
            summary["text"] = report.text[:400] + "..."
            summary["passes_one_page_checks"] = report.passes(max_pages=1)
            result[f"{label}_pdf"] = summary
        result["old_text_absent_from_edited_pdf"] = target.text not in result["edited_pdf"]["text"]
    except ConversionUnavailableError as exc:
        result["pdf"] = f"SKIPPED: {exc}"

    (OUT / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    ok = result["master_unchanged"] and result["formatting_ok"]
    if "edited_pdf" in result:
        ok = ok and result["edited_pdf"]["passes_one_page_checks"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
