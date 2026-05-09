from __future__ import annotations
import logging
import time
from typing import TYPE_CHECKING

from .models import CodeUnit

logger = logging.getLogger(__name__)

try:
    import torch
    from sentence_transformers import SentenceTransformer
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False
    torch = None  # type: ignore[assignment]
    SentenceTransformer = None  # type: ignore[assignment]

_model: "SentenceTransformer | None" = None
_device: str = "cpu"


def _get_device() -> str:
    if _TORCH_AVAILABLE and torch.cuda.is_available():
        return "cuda"
    return "cpu"


def get_device_name() -> str:
    """Return a human-readable device name for display in stats."""
    if not _TORCH_AVAILABLE:
        return "cpu (torch not installed)"
    if not torch.cuda.is_available():
        return "cpu"
    try:
        return torch.cuda.get_device_name(0)
    except Exception:
        return "cuda"


def is_gpu_available() -> bool:
    """Return True if a GPU (CUDA/ROCm) is available."""
    return _TORCH_AVAILABLE and torch.cuda.is_available()


def _load_model(model_name: str) -> "SentenceTransformer":
    global _model, _device
    if _model is not None:
        return _model
    _device = _get_device()
    logger.info("Loading embedding model '%s' on %s", model_name, _device)
    _model = SentenceTransformer(model_name, trust_remote_code=True)
    _model = _model.to(_device)
    return _model


def _unit_text(unit: CodeUnit) -> str:
    parts = [unit.name]
    if unit.docstring:
        parts.append(unit.docstring)
    if unit.source_snippet:
        parts.append(unit.source_snippet[:500])
    return "\n".join(parts)


def embed_units(units: list[CodeUnit], model_name: str = "nomic-ai/nomic-embed-text-v1.5") -> tuple[list[CodeUnit], float]:
    """
    Generate embeddings for all units. Returns (units_with_embeddings, elapsed_ms).
    If torch/sentence-transformers are not installed, returns units unchanged with 0ms.
    """
    if not _TORCH_AVAILABLE:
        logger.info("Torch not available — skipping embedding generation")
        return units, 0.0

    model = _load_model(model_name)
    texts = [_unit_text(u) for u in units]

    start = time.perf_counter()
    embeddings = model.encode(
        texts,
        batch_size=64,
        show_progress_bar=False,
        convert_to_numpy=True,
        device=_device,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    for unit, emb in zip(units, embeddings):
        unit.embedding = emb.tolist()

    logger.info("Embedded %d units in %.1fms on %s", len(units), elapsed_ms, _device)
    return units, elapsed_ms


def find_similar(units: list[CodeUnit], unit_id: str, top_k: int = 5) -> list[tuple[str, float]]:
    """Return top_k most similar units by cosine similarity. Requires embeddings."""
    if not _TORCH_AVAILABLE:
        return []

    target = next((u for u in units if u.id == unit_id), None)
    if target is None or target.embedding is None:
        return []

    import torch as th
    target_vec = th.tensor(target.embedding)
    candidates = [(u.id, u.embedding) for u in units if u.id != unit_id and u.embedding is not None]
    if not candidates:
        return []

    ids, vecs = zip(*candidates)
    matrix = th.tensor(vecs)
    scores = th.nn.functional.cosine_similarity(target_vec.unsqueeze(0), matrix)
    top_indices = scores.topk(min(top_k, len(scores))).indices.tolist()
    return [(ids[i], float(scores[i])) for i in top_indices]
