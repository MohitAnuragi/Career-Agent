"""Local sentence embeddings (BGE-small via ONNX/fastembed). No API cost, deterministic for evals."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable

import numpy as np

from app.config import get_settings

# BGE models retrieve better when short queries carry this instruction; passages are embedded as-is.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MAX_CHARS = 2000  # ~512 tokens; the head of a posting (title, team, requirements) carries most signal


# Filled by scripts/fetch_model.py at build time (Vercel). Absent locally/in Docker, which use the download cache.
BUNDLED_MODEL = Path(__file__).resolve().parent.parent / "models" / "bge-small-en-v1.5"


@lru_cache
def _model():
    from fastembed import TextEmbedding

    s = get_settings()
    if (BUNDLED_MODEL / "model_optimized.onnx").exists():
        # Load straight from the deployment bundle: no network, about a second on a cold start.
        return TextEmbedding(model_name=s.embedding_model, specific_model_path=str(BUNDLED_MODEL),
                             cache_dir=s.fastembed_cache)
    return TextEmbedding(model_name=s.embedding_model, cache_dir=s.fastembed_cache)


def embed_passages(texts: Iterable[str], batch_size: int = 16) -> list[np.ndarray]:
    # Small batches keep peak memory low (free-tier hosts have ~512 MB) and waste less on padding.
    texts = [t[:MAX_CHARS] for t in texts]
    if not texts:
        return []
    return list(_model().embed(texts, batch_size=batch_size))


def embed_query(text: str) -> np.ndarray:
    return next(iter(_model().embed([QUERY_PREFIX + text[:MAX_CHARS]])))


def job_passage(title: str, company: str, location: str, department: str, description: str) -> str:
    return f"{title} at {company}. {department}. {location}.\n{description}"
