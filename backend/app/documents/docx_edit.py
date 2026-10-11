"""Template-aware DOCX editing.

Edits replace the *text* of one paragraph while keeping the template's own
formatting: paragraph properties (``w:pPr`` - bullets, indents, spacing) are
never touched, and new runs clone run properties (``w:rPr``) that already
exist in the paragraph. Layouts we cannot edit safely raise
``UnsupportedLayoutError`` instead of being guessed at.
"""

from __future__ import annotations

import copy
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

# Elements inside a paragraph that we refuse to rewrite, because replacing the
# runs would silently drop links, fields, revisions, or embedded objects.
_UNSUPPORTED_CHILDREN = {
    qn("w:hyperlink"): "hyperlink",
    qn("w:fldSimple"): "field",
    qn("w:ins"): "tracked insertion",
    qn("w:del"): "tracked deletion",
    qn("w:smartTag"): "smart tag",
    qn("w:sdt"): "content control",
}
_UNSUPPORTED_RUN_CHILDREN = {
    qn("w:fldChar"): "complex field",
    qn("w:drawing"): "drawing",
    qn("w:pict"): "legacy picture",
    qn("w:object"): "embedded object",
    qn("w:footnoteReference"): "footnote reference",
}
_ALLOWED_PARAGRAPH_CHILDREN = {
    qn("w:pPr"),
    qn("w:r"),
    qn("w:proofErr"),
    qn("w:bookmarkStart"),
    qn("w:bookmarkEnd"),
}


class UnsupportedLayoutError(ValueError):
    """The paragraph uses structure the editor cannot preserve exactly."""


class StaleEditError(ValueError):
    """The paragraph's current text no longer matches what the caller expected."""


@dataclass(frozen=True)
class TextSegment:
    text: str
    bold: bool = False


@dataclass(frozen=True)
class ParagraphRef:
    index: int  # position among all body paragraphs, in document order
    text: str
    is_bullet: bool


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def body_paragraphs(document: DocxDocument) -> list[Paragraph]:
    """All paragraphs in the body, including table cells, in document order.

    Paragraphs inside text boxes are excluded; they are positioned shapes whose
    layout we do not reconstruct.
    """
    body = document.element.body
    paragraphs = []
    for p in body.iter(qn("w:p")):
        if any(a.tag == qn("w:txbxContent") for a in p.iterancestors()):
            continue
        paragraphs.append(Paragraph(p, document._body))
    return paragraphs


def _style_has_numbering(document: DocxDocument, style_id: str | None) -> bool:
    seen: set[str] = set()
    styles = document.styles.element
    while style_id and style_id not in seen:
        seen.add(style_id)
        style = next(
            (s for s in styles.iterchildren(qn("w:style")) if s.get(qn("w:styleId")) == style_id),
            None,
        )
        if style is None:
            return False
        if style.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None:
            return True
        based_on = style.find(qn("w:basedOn"))
        style_id = based_on.get(qn("w:val")) if based_on is not None else None
    return False


def is_bullet(document: DocxDocument, paragraph: Paragraph) -> bool:
    ppr = paragraph._p.pPr
    if ppr is not None and ppr.find(qn("w:numPr")) is not None:
        return True
    style_id = None
    if ppr is not None and ppr.pStyle is not None:
        style_id = ppr.pStyle.val
    return _style_has_numbering(document, style_id)


def list_paragraphs(document: DocxDocument) -> list[ParagraphRef]:
    return [
        ParagraphRef(index=i, text=p.text, is_bullet=is_bullet(document, p))
        for i, p in enumerate(body_paragraphs(document))
    ]


def _check_supported(paragraph: Paragraph) -> None:
    for child in paragraph._p:
        name = _UNSUPPORTED_CHILDREN.get(child.tag)
        if name:
            raise UnsupportedLayoutError(f"paragraph contains a {name}")
        if child.tag not in _ALLOWED_PARAGRAPH_CHILDREN:
            raise UnsupportedLayoutError(f"paragraph contains unsupported element {child.tag}")
    for run in paragraph._p.findall(qn("w:r")):
        for child in run.iter():
            name = _UNSUPPORTED_RUN_CHILDREN.get(child.tag)
            if name:
                raise UnsupportedLayoutError(f"paragraph contains a {name}")


def _run_is_bold(run_element) -> bool:
    rpr = run_element.find(qn("w:rPr"))
    if rpr is None:
        return False
    bold = rpr.find(qn("w:b"))
    if bold is None:
        return False
    return bold.get(qn("w:val"), "true").lower() not in {"0", "false", "off"}


def _with_bold(rpr, bold: bool):
    """Return a copy of ``rpr`` with direct bold forced on or off."""
    rpr = copy.deepcopy(rpr) if rpr is not None else Document().element.makeelement(qn("w:rPr"), {})
    for tag in ("w:b", "w:bCs"):
        existing = rpr.find(qn(tag))
        if existing is not None:
            rpr.remove(existing)
    # Schema order puts rStyle and rFonts before b.
    anchor_index = sum(1 for c in rpr if c.tag in {qn("w:rStyle"), qn("w:rFonts")})
    rpr.insert(anchor_index, rpr.makeelement(qn("w:b"), {} if bold else {qn("w:val"): "0"}))
    return rpr


def _template_rprs(paragraph: Paragraph):
    """Pick existing run properties to clone for plain and bold text."""
    runs = [r for r in paragraph._p.findall(qn("w:r")) if r.find(qn("w:t")) is not None]
    if not runs:
        raise UnsupportedLayoutError("paragraph has no text runs to use as a formatting template")
    plain = next((r for r in runs if not _run_is_bold(r)), None)
    bold = next((r for r in runs if _run_is_bold(r)), None)

    def rpr_of(run):
        rpr = run.find(qn("w:rPr"))
        return copy.deepcopy(rpr) if rpr is not None else None

    plain_rpr = rpr_of(plain) if plain is not None else _with_bold(rpr_of(bold), False)
    bold_rpr = rpr_of(bold) if bold is not None else _with_bold(plain_rpr, True)
    return plain_rpr, bold_rpr


def replace_paragraph_text(
    paragraph: Paragraph, expected_text: str, segments: list[TextSegment]
) -> None:
    """Replace a paragraph's text, keeping its paragraph and run formatting."""
    if paragraph.text != expected_text:
        raise StaleEditError("paragraph text changed since it was read")
    if not segments or not any(s.text.strip() for s in segments):
        raise ValueError("replacement text must not be empty")
    _check_supported(paragraph)

    plain_rpr, bold_rpr = _template_rprs(paragraph)
    p = paragraph._p
    for run in p.findall(qn("w:r")) + p.findall(qn("w:proofErr")):
        p.remove(run)

    for segment in segments:
        if not segment.text:
            continue
        run = p.makeelement(qn("w:r"), {})
        template = bold_rpr if segment.bold else plain_rpr
        if template is not None:
            run.append(copy.deepcopy(template))
        text = run.makeelement(qn("w:t"), {})
        text.text = segment.text
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        run.append(text)
        # Keep runs before a trailing bookmarkEnd so bookmarks stay balanced.
        trailing = p.find(qn("w:bookmarkEnd"))
        if trailing is not None and trailing.getnext() is None:
            trailing.addprevious(run)
        else:
            p.append(run)


def edit_copy(
    master_path: Path,
    output_path: Path,
    paragraph_index: int,
    expected_text: str,
    segments: list[TextSegment],
) -> None:
    """Write an edited copy of ``master_path``; the master itself is never modified."""
    master_path = master_path.resolve()
    output_path = output_path.resolve()
    if master_path == output_path:
        raise ValueError("output must differ from the master file")
    before = sha256_file(master_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(master_path, output_path)
    document = Document(str(output_path))
    paragraphs = body_paragraphs(document)
    if not 0 <= paragraph_index < len(paragraphs):
        raise IndexError("paragraph index out of range")
    replace_paragraph_text(paragraphs[paragraph_index], expected_text, segments)
    document.save(str(output_path))

    if sha256_file(master_path) != before:  # defensive: should be impossible
        raise RuntimeError("master file changed during editing")
