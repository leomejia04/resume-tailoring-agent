"""Download the pinned embedding model into backend/.models (run once per machine).

Usage (from backend/):
    .venv/Scripts/python -m scripts.fetch_embedding_model

Fetches only the files listed in app.retrieval.embedder.FILES from the pinned
commit on huggingface.co (safetensors weights, configs, tokenizer; never the
pickle-based pytorch_model.bin), streams each into the model folder, and keeps
it only if its SHA-256 matches the pinned value. Re-running skips files that
are already present and valid. Set USE_OS_TRUST_STORE=true in .env if local
antivirus re-signs HTTPS (verification stays on).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import httpx

from app.config import get_settings
from app.retrieval.embedder import (
    FILES,
    MODEL_ID,
    MODEL_REVISION,
    sha256_file,
    snapshot_dir,
    verify_snapshot,
)

BASE = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/"
MAX_FILE_BYTES = 200 * 1024 * 1024


def _download(client: httpx.Client, name: str, target: Path) -> None:
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    size = 0
    with client.stream("GET", BASE + name) as response:
        response.raise_for_status()
        if response.url.scheme != "https":
            raise SystemExit(f"refusing non-HTTPS download for {name}")
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise SystemExit(f"{name} is larger than expected")
                digest.update(chunk)
                handle.write(chunk)
    if digest.hexdigest() != FILES[name]:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"{name} does not match its pinned SHA-256; nothing was kept")
    partial.replace(target)


def main() -> None:
    settings = get_settings()
    if settings.use_os_trust_store:
        import truststore

        truststore.inject_into_ssl()
    directory = snapshot_dir(settings)
    with httpx.Client(follow_redirects=True, timeout=60, max_redirects=5) as client:
        for name, expected in FILES.items():
            target = directory / name
            if target.is_file() and sha256_file(target) == expected:
                print(f"ok      {name}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            print(f"fetch   {name}")
            _download(client, name, target)
    verify_snapshot(directory)
    print(f"Model {MODEL_ID}@{MODEL_REVISION[:12]} verified in {directory}")


if __name__ == "__main__":
    sys.exit(main())
