# Resume tailoring app

A hosted, multi-user application for creating truthful, tailored, one-page PDF
resumes. The full specification and build progress live in [CLAUDE.md](CLAUDE.md).

**Current state:** milestone 1 (document proof) is complete: one bullet of a
fictional master DOCX is edited in a copy with formatting preserved, rendered
to PDF in a sandboxed LibreOffice container, and inspected. There is no web
app or API yet (milestone 2).

## Repository layout

```
backend/app/documents/   DOCX editing, formatting verification, conversion, PDF inspection
backend/tests/           pytest suite
backend/scripts/         document_proof.py (milestone 1 end-to-end proof)
docker/converter/        LibreOffice + fonts converter image
docs/decisions/          architecture decision records
docs/learning-log.md     plain-language notes on concepts used
```

## Prerequisites (Windows)

- Python 3.12+ (developed on 3.13)
- Docker Desktop with the WSL 2 engine (used to run LibreOffice; LibreOffice
  does not need to be installed on Windows)
- Git

## Setup and checks

Commands below were run from Git Bash on Windows. In PowerShell, use
backslashes (`.venv\Scripts\python`).

Build the converter image (from the repository root):

```bash
docker build -t resume-converter:dev docker/converter
```

Create the backend environment and install the hash-locked dependencies (from `backend/`):

```bash
python -m venv .venv
.venv/Scripts/pip install --require-hashes -r requirements.txt -r requirements-dev.txt
```

Run the tests, lint, and format check (from `backend/`):

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
```

Run the document proof (from `backend/`). It writes the fictional master and
edited DOCX/PDF files, page images, and `report.json` to `backend/out/proof/`
(git-ignored):

```bash
.venv/Scripts/python -m scripts.document_proof
```

## Converter configuration

Optional environment variables (see [.env.example](.env.example)):

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONVERTER` | `auto` | `auto` uses a local `soffice` if found, else the Docker image; or force `local` / `docker` |
| `CONVERTER_IMAGE` | `resume-converter:dev` | Docker image used by the `docker` backend |
| `SOFFICE_PATH` | (unset) | Path to a local `soffice` executable |
| `CONVERSION_TIMEOUT_SECONDS` | `60` | Hard limit per conversion |

## Updating dependencies

Edit `backend/requirements.in` or `requirements-dev.in`, then re-lock (slow:
hashes every platform's wheels):

```bash
.venv/Scripts/pip-compile --generate-hashes --strip-extras -o requirements.txt requirements.in
```

```bash
.venv/Scripts/pip-compile --generate-hashes --strip-extras -o requirements-dev.txt requirements-dev.in
```
