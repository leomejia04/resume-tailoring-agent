"""Independent verification that an edited DOCX kept the master's formatting.

The editor is not trusted to report its own success: this module compares the
two packages part by part using canonical XML, so serialization noise (quote
style, namespace prefixes order) is ignored while real changes are reported.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from app.documents.docx_edit import body_paragraphs

_DOCUMENT_PART = "word/document.xml"


@dataclass
class FormattingReport:
    changed_parts: list[str] = field(default_factory=list)
    section_changes: list[str] = field(default_factory=list)
    paragraph_changes: list[str] = field(default_factory=list)
    target_ppr_preserved: bool = False
    target_run_formats: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.changed_parts
            and not self.section_changes
            and not self.paragraph_changes
            and self.target_ppr_preserved
        )


def _c14n(element) -> bytes:
    if element is None:
        return b""
    return etree.tostring(element, method="c14n")


def _canonical_parts(path: Path) -> dict[str, bytes]:
    parts: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            if name.endswith((".xml", ".rels")):
                parser = etree.XMLParser(resolve_entities=False, no_network=True)
                data = etree.tostring(etree.fromstring(data, parser), method="c14n")
            parts[name] = data
    return parts


def _run_signature(p) -> list[tuple[bytes, str]]:
    sig = []
    for run in p.findall(qn("w:r")):
        text = "".join(t.text or "" for t in run.iter(qn("w:t")))
        sig.append((_c14n(run.find(qn("w:rPr"))), text))
    return sig


def describe_rpr(rpr) -> str:
    if rpr is None:
        return "inherited"
    bits = []
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is not None and fonts.get(qn("w:ascii")):
        bits.append(fonts.get(qn("w:ascii")))
    size = rpr.find(qn("w:sz"))
    if size is not None:
        bits.append(f"{int(size.get(qn('w:val'))) / 2:g}pt")
    bold = rpr.find(qn("w:b"))
    if bold is not None and bold.get(qn("w:val"), "true") not in {"0", "false"}:
        bits.append("bold")
    return " ".join(bits) or "inherited"


def compare_formatting(original: Path, edited: Path, target_index: int) -> FormattingReport:
    report = FormattingReport()

    original_parts = _canonical_parts(original)
    edited_parts = _canonical_parts(edited)
    for name in sorted(set(original_parts) | set(edited_parts)):
        if name == _DOCUMENT_PART:
            continue
        if original_parts.get(name) != edited_parts.get(name):
            report.changed_parts.append(name)

    doc_a, doc_b = Document(str(original)), Document(str(edited))
    sect_a = [_c14n(s) for s in doc_a.element.body.iter(qn("w:sectPr"))]
    sect_b = [_c14n(s) for s in doc_b.element.body.iter(qn("w:sectPr"))]
    if sect_a != sect_b:
        report.section_changes.append("page size, margins, or columns changed")

    paras_a, paras_b = body_paragraphs(doc_a), body_paragraphs(doc_b)
    if len(paras_a) != len(paras_b):
        report.paragraph_changes.append(
            f"paragraph count changed from {len(paras_a)} to {len(paras_b)}"
        )
        return report

    for i, (pa, pb) in enumerate(zip(paras_a, paras_b, strict=True)):
        same_ppr = _c14n(pa._p.pPr) == _c14n(pb._p.pPr)
        if i == target_index:
            report.target_ppr_preserved = same_ppr
            report.target_run_formats = [
                describe_rpr(r.find(qn("w:rPr"))) for r in pb._p.findall(qn("w:r"))
            ]
            continue
        if not same_ppr:
            report.paragraph_changes.append(f"paragraph {i}: paragraph formatting changed")
        if _run_signature(pa._p) != _run_signature(pb._p):
            report.paragraph_changes.append(f"paragraph {i}: text or run formatting changed")
    return report
