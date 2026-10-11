"""DOCX -> PDF conversion with headless LibreOffice.

Two backends share the same contract:

* ``local`` runs an installed ``soffice`` (used inside the production worker
  container, where LibreOffice and fonts are installed).
* ``docker`` streams the DOCX into a disposable converter container on stdin and
  reads the PDF from stdout (used on development hosts such as Windows). The
  container has no network, no host mounts, a read-only filesystem, a non-root
  user, no capabilities, and memory/CPU/process limits.

Either way: argument lists only (never a shell string), no user-supplied
filenames, bounded input size, and hard timeouts.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

MAX_DOCX_BYTES = 20 * 1024 * 1024
# Extra time the outer timeout allows for container start/stop around soffice.
_DOCKER_OVERHEAD_SECONDS = 30


class ConversionUnavailableError(RuntimeError):
    """No configured converter backend is available."""


class ConversionError(RuntimeError):
    """A converter ran but did not produce a valid PDF."""


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


def converter_image() -> str:
    return os.environ.get("CONVERTER_IMAGE", "resume-converter:dev").strip()


@functools.cache
def _docker_image_available(image: str) -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        result = subprocess.run(
            [docker, "image", "inspect", image],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def resolve_backend() -> str | None:
    """Return "local", "docker", or None, honoring the CONVERTER setting."""
    choice = os.environ.get("CONVERTER", "auto").strip().lower()
    if choice not in {"auto", "local", "docker"}:
        raise ValueError("CONVERTER must be auto, local, or docker")
    if choice in {"auto", "local"} and find_soffice() is not None:
        return "local"
    if choice in {"auto", "docker"} and _docker_image_available(converter_image()):
        return "docker"
    return None


def _convert_local(data: bytes, timeout_seconds: float) -> bytes:
    soffice = find_soffice()
    if soffice is None:
        raise ConversionUnavailableError("LibreOffice was not found")
    with tempfile.TemporaryDirectory(prefix="convert-") as work:
        work_dir = Path(work)
        source = work_dir / "input.docx"
        source.write_bytes(data)
        args = [
            str(soffice),
            f"-env:UserInstallation={(work_dir / 'profile').as_uri()}",
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
            subprocess.run(args, cwd=work_dir, capture_output=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(f"conversion timed out after {timeout_seconds:g}s") from exc
        produced = work_dir / "input.pdf"
        return produced.read_bytes() if produced.is_file() else b""


def _convert_docker(data: bytes, timeout_seconds: float) -> bytes:
    docker = shutil.which("docker")
    if docker is None:
        raise ConversionUnavailableError("Docker was not found")
    name = f"convert-{uuid.uuid4().hex}"
    args = [
        docker, "run", "--rm", "-i",
        "--name", name,
        "--network", "none",
        "--memory", "1g", "--memory-swap", "1g",
        "--cpus", "1",
        "--pids-limit", "256",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",  # noqa: S108 - private tmpfs inside the container
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-e", f"CONVERT_TIMEOUT_SECONDS={max(1, int(timeout_seconds))}",
        converter_image(),
    ]  # fmt: skip
    try:
        result = subprocess.run(
            args,
            input=data,
            capture_output=True,
            timeout=timeout_seconds + _DOCKER_OVERHEAD_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        # Killing the docker client does not stop the container; remove it explicitly.
        subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=30)
        raise ConversionError(f"conversion timed out after {timeout_seconds:g}s") from exc
    if result.returncode != 0:
        raise ConversionError(f"converter exited with status {result.returncode}")
    return result.stdout


def docx_to_pdf(docx_path: Path, pdf_path: Path, timeout_seconds: float | None = None) -> Path:
    if timeout_seconds is None:
        timeout_seconds = float(os.environ.get("CONVERSION_TIMEOUT_SECONDS", "60"))
    if docx_path.stat().st_size > MAX_DOCX_BYTES:
        raise ConversionError(f"DOCX is larger than {MAX_DOCX_BYTES} bytes")

    backend = resolve_backend()
    if backend is None:
        raise ConversionUnavailableError(
            "No converter available: build the Docker converter image or install "
            "LibreOffice (see README)."
        )
    data = docx_path.read_bytes()
    convert = _convert_local if backend == "local" else _convert_docker
    pdf = convert(data, timeout_seconds)
    if not pdf.startswith(b"%PDF-"):
        raise ConversionError("converter did not produce a PDF")

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(pdf)
    return pdf_path
