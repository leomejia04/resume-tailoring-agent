"""A deterministic stand-in embedder for tests (never used by the app).

Each word is hashed into one of 384 dimensions, so texts sharing words are
similar and unrelated texts are not: enough to test indexing, ownership, and
ranking plumbing without loading the real model. Tests about real semantic
quality use the pinned model and skip when it is not installed.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from app.retrieval.embedder import DIMENSION, MODEL_ID, MODEL_REVISION

_WORD = re.compile(r"\S+")


class HashEmbedder:
    model_id = MODEL_ID
    revision = MODEL_REVISION
    dimension = DIMENSION

    def __init__(self, max_tokens: int = 256) -> None:
        self.max_tokens = max_tokens
        self.calls: list[int] = []

    def count_tokens(self, text: str) -> int:
        return len(_WORD.findall(text))

    def windows(self, text: str, max_tokens: int, overlap: int) -> list[str]:
        words = _WORD.findall(text)
        if len(words) <= max_tokens:
            return [text]
        step = max(1, max_tokens - overlap)
        spans = []
        for start in range(0, len(words), step):
            spans.append(" ".join(words[start : start + max_tokens]))
            if start + max_tokens >= len(words):
                break
        return spans

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        vectors = []
        for text in texts:
            vector = [0.0] * DIMENSION
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                slot = int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMENSION
                vector[slot] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
        return vectors
