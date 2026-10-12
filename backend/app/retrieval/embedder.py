"""Local sentence embeddings for retrieval (milestone 7, ADR 0010).

The model runs in this process on CPU from a pinned snapshot that
``scripts.fetch_embedding_model`` downloads once. Loading never touches the
network, never runs repository code (no ``trust_remote_code``), and refuses
files whose SHA-256 differs from the pinned values below, so a swapped or
corrupted file cannot load silently. Only safetensors weights are fetched,
never pickle-based ``pytorch_model.bin``.

Vectors are L2-normalized (the model's own Normalize module), so cosine
distance equals one minus the dot product.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.config import Settings

logger = logging.getLogger("app.retrieval")

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
# Commit on huggingface.co (checked 2026-10-11; license apache-2.0).
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
DIMENSION = 384
# The model was trained on inputs up to 256 word pieces and truncates beyond.
MAX_TOKENS = 256
# File -> SHA-256 at MODEL_REVISION: the weights' hash is the LFS object ID that
# huggingface.co lists for this commit; the small files were downloaded from the
# commit and matched against its git object IDs before hashing (2026-10-11).
FILES: dict[str, str] = {
    "1_Pooling/config.json": "4be450dde3b0273bb9787637cfbd28fe04a7ba6ab9d36ac48e92b11e350ffc23",
    "config.json": "953f9c0d463486b10a6871cc2fd59f223b2c70184f49815e7efbcab5d8908b41",
    "config_sentence_transformers.json": (
        "061ca9d39661d6c6d6de5ba27f79a1cd5770ea247f8d46412a68a498dc5ac9f3"
    ),
    "model.safetensors": "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db",
    "modules.json": "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
    "sentence_bert_config.json": "fc1993fde0a95c24ec6c022539d41cf6e2f7c9721e5415d6fb6897472a9cd4b7",
    "special_tokens_map.json": "303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
    "tokenizer_config.json": "acb92769e8195aabd29b7b2137a9e6d6e25c476a4f15aa4355c233426c61576b",
    "vocab.txt": "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
}


class EmbeddingUnavailableError(RuntimeError):
    """The model is not installed, fails verification, or cannot load."""


class Embedder(Protocol):
    model_id: str
    revision: str
    dimension: int
    max_tokens: int

    def count_tokens(self, text: str) -> int: ...

    def windows(self, text: str, max_tokens: int, overlap: int) -> list[str]: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def snapshot_dir(settings: Settings) -> Path:
    return settings.embedding_model_dir / MODEL_REVISION


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_snapshot(directory: Path) -> None:
    for name, expected in FILES.items():
        path = directory / name
        if not path.is_file():
            raise EmbeddingUnavailableError(f"missing model file {name}")
        if sha256_file(path) != expected:
            raise EmbeddingUnavailableError(f"model file {name} does not match its pinned hash")


@dataclass
class SentenceTransformerEmbedder:
    """Wraps a loaded SentenceTransformer; one shared instance per process."""

    _model: object
    _lock: threading.Lock
    model_id: str = MODEL_ID
    revision: str = MODEL_REVISION
    dimension: int = DIMENSION
    max_tokens: int = MAX_TOKENS

    @property
    def _tokenizer(self):  # noqa: ANN202
        return self._model.tokenizer  # type: ignore[attr-defined]

    def count_tokens(self, text: str) -> int:
        return len(self._tokenizer(text, add_special_tokens=False)["input_ids"])

    def windows(self, text: str, max_tokens: int, overlap: int) -> list[str]:
        """Split text into overlapping spans of at most ``max_tokens`` word pieces."""
        encoded = self._tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        offsets = encoded["offset_mapping"]
        if len(offsets) <= max_tokens:
            return [text]
        step = max(1, max_tokens - overlap)
        spans: list[str] = []
        for start in range(0, len(offsets), step):
            window = offsets[start : start + max_tokens]
            spans.append(text[window[0][0] : window[-1][1]].strip())
            if start + max_tokens >= len(offsets):
                break
        return [s for s in spans if s]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:  # torch inference is not guaranteed to be thread-safe
            vectors = self._model.encode(  # type: ignore[attr-defined]
                list(texts),
                batch_size=32,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        return [[float(x) for x in row] for row in vectors]


_loaded: dict[Path, SentenceTransformerEmbedder] = {}
_load_lock = threading.Lock()


def load_embedder(settings: Settings) -> SentenceTransformerEmbedder:
    """Load (once per process) and verify the pinned model, or raise EmbeddingUnavailableError."""
    directory = snapshot_dir(settings)
    with _load_lock:
        if directory in _loaded:
            return _loaded[directory]
        verify_snapshot(directory)
        try:
            import torch
            from sentence_transformers import SentenceTransformer

            torch.set_num_threads(settings.embedding_threads)
            model = SentenceTransformer(
                str(directory),
                device="cpu",
                local_files_only=True,
                trust_remote_code=False,
            )
        except Exception as exc:  # import or load failure: retrieval falls back
            logger.error("embedding model failed to load error=%s", type(exc).__name__)
            raise EmbeddingUnavailableError("the embedding model could not be loaded") from exc
        dimension = model.get_sentence_embedding_dimension()
        if dimension != DIMENSION:
            raise EmbeddingUnavailableError(f"unexpected embedding dimension {dimension}")
        model.max_seq_length = MAX_TOKENS
        embedder = SentenceTransformerEmbedder(model, threading.Lock())
        _loaded[directory] = embedder
        logger.info("embedding model loaded revision=%s", MODEL_REVISION[:12])
        return embedder
