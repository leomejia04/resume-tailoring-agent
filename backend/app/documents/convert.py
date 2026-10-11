"""DOCX -> PDF conversion with headless LibreOffice.

Each conversion runs in a fresh temporary directory with its own throwaway
LibreOffice profile, a fixed input filename (never a user-supplied name), an
argument list (never a shell string), and a hard timeout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class ConversionUnavailableError(RuntimeError):
    """LibreOffice is not installed or not configured."""


class ConversionError(RuntimeError):
    """LibreOffice ran but did not produce a PDF."""


def find_soffice() -> Path | None:
    configured = os.environ.get("SOFFICE_PATH", "").strip()
    candidates = [configured] if configured else []
    candidates += [shutil.which("soffice") or "", shutil.which("libreoffice") or ""]
    if sys.platform == "win32":
        candidates.append(r"C:\Program Files\LibreOffice\program\soffice.exe")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            path = Path(candidate)
            # On Windows, soffice.com is the console launcher that waits for completion.
            console = path.with_suffix(".com")
            return console if sys.platform == "win32" and console.is_file() else path
    return None


def docx_to_pdf(docx_path: Path, pdf_path: Path, timeout_seconds: float | None = None) -> Path:
    soffice = find_soffice()
    if soffice is None:
        raise ConversionUnavailableError(
            "LibreOffice was not found. Install it or set SOFFICE_PATH (see README)."
        )
    if timeout_seconds is None:
        timeout_seconds = float(os.environ.get("CONVERSION_TIMEOUT_SECONDS", "60"))

    with tempfile.TemporaryDirectory(prefix="convert-") as work:
        work_dir = Path(work)
        source = work_dir / "input.docx"
        shutil.copyfile(docx_path, source)
        profile = (work_dir / "profile").as_uri()
        args = [
            str(soffice),
            f"-env:UserInstallation={profile}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--convert-to",
            "pdf",
            "--outdir",
            str(work_dir),
            str(source),
        ]
        try:
            subprocess.run(
                args,
                cwd=work_dir,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(f"conversion timed out after {timeout_seconds:g}s") from exc

        produced = work_dir / "input.pdf"
        if not produced.is_file() or produced.stat().st_size == 0:
            raise ConversionError("LibreOffice did not produce a PDF")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(produced, pdf_path)
    return pdf_path
