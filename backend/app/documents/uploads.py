"""Validate uploaded master resumes before anything else touches them.

The file type is decided from the bytes, never from the client's
Content-Type. DOCX files are ZIP archives, so they are checked for ZIP-bomb
shapes, path tricks, macros, and embedded objects before being parsed. PDFs
must not be encrypted. Nothing in an upload is ever executed.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .doc and encrypted OOXML

MAX_ZIP_ENTRIES = 1000
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_PDF_PAGES = 10
_FORBIDDEN_PARTS = re.compile(r"(vbaProject\.bin|vbaData\.xml|/embeddings/|/activeX/)", re.I)
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9 ._()\-]+")


class UploadRejectedError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedUpload:
    kind: str  # "docx" | "pdf"
    content_type: str
    sha256: str
    size: int
    label: str


def safe_label(filename: str | None) -> str:
    """A display-only name: no directories, only plain characters, bounded length."""
    name = PureWindowsPath(PurePosixPath(filename or "").name).name
    name = _SAFE_LABEL.sub("_", name).strip(" ._") or "resume"
    return name[:120]


def _check_docx(data: bytes) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadRejectedError(
            "invalid_file", "This file is not a valid Word document."
        ) from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise UploadRejectedError(
                "invalid_file", "This Word document has too many internal parts."
            )
        total = 0
        names = set()
        for entry in entries:
            name = entry.filename
            if name.startswith(("/", "\\")) or ".." in PurePosixPath(name).parts or ":" in name:
                raise UploadRejectedError(
                    "invalid_file", "This Word document has an unsafe structure."
                )
            if entry.flag_bits & 0x1:
                raise UploadRejectedError(
                    "encrypted_file", "Password-protected documents are not supported."
                )
            total += entry.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise UploadRejectedError(
                    "invalid_file", "This Word document expands to an unsafe size."
                )
            if (
                entry.compress_size
                and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO
                and entry.file_size > 1024 * 1024
            ):
                raise UploadRejectedError(
                    "invalid_file", "This Word document is compressed in an unsafe way."
                )
            if _FORBIDDEN_PARTS.search("/" + name):
                raise UploadRejectedError(
                    "unsupported_content",
                    "Documents with macros or embedded objects are not supported. "
                    "Save a plain .docx copy and upload that.",
                )
            names.add(name)
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise UploadRejectedError("invalid_file", "This file is not a valid Word document.")
        content_types = archive.read("[Content_Types].xml")
        if b"macroEnabled" in content_types or b"vbaProject" in content_types:
            raise UploadRejectedError(
                "unsupported_content",
                "Macro-enabled documents are not supported. "
                "Save a plain .docx copy and upload that.",
            )


def _check_pdf(data: bytes) -> None:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if pdf.doc.encryption is not None:
                raise UploadRejectedError(
                    "encrypted_file", "Password-protected PDFs are not supported."
                )
            if len(pdf.pages) == 0:
                raise UploadRejectedError("invalid_file", "This PDF has no pages.")
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise UploadRejectedError(
                    "invalid_file", f"Resume PDFs can have at most {MAX_PDF_PAGES} pages."
                )
    except UploadRejectedError:
        raise
    except (PDFPasswordIncorrect, PDFEncryptionError) as exc:
        raise UploadRejectedError(
            "encrypted_file", "Password-protected PDFs are not supported."
        ) from exc
    except (PDFSyntaxError, ValueError, KeyError, TypeError) as exc:
        raise UploadRejectedError("invalid_file", "This file is not a readable PDF.") from exc


def validate_upload(data: bytes, filename: str | None, max_bytes: int) -> ValidatedUpload:
    if not data:
        raise UploadRejectedError("empty_file", "The file is empty.")
    if len(data) > max_bytes:
        raise UploadRejectedError(
            "file_too_large", f"Files can be at most {max_bytes // (1024 * 1024)} MB."
        )
    extension = PurePosixPath((filename or "").lower()).suffix

    if data.startswith(OLE_SIGNATURE):
        raise UploadRejectedError(
            "unsupported_format",
            "This looks like an old .doc file or a password-protected document. "
            "Save it as .docx and try again.",
        )
    if data.startswith(b"PK\x03\x04"):
        if extension != ".docx":
            raise UploadRejectedError("unsupported_format", "Upload a .docx or .pdf file.")
        _check_docx(data)
        kind, content_type = "docx", DOCX_MIME
    elif data.startswith(b"%PDF-"):
        if extension != ".pdf":
            raise UploadRejectedError("unsupported_format", "Upload a .docx or .pdf file.")
        _check_pdf(data)
        kind, content_type = "pdf", PDF_MIME
    else:
        raise UploadRejectedError("unsupported_format", "Upload a .docx or .pdf file.")

    return ValidatedUpload(
        kind=kind,
        content_type=content_type,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        label=safe_label(filename),
    )
