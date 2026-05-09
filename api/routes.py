from __future__ import annotations
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.embedder import find_similar, get_device_name, is_gpu_available
from core.explainer import explain_unit
from core.models import ProjectGraph

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Populated by api/main.py on startup.
_current_graph: ProjectGraph | None = None
_current_graph_json: dict | None = None
_analysis_error: str | None = None
_analysis_done: bool = False
_dep_index: dict[str, list[str]] = {}      # source_id → [target names]
_used_by_index: dict[str, list[str]] = {}  # target_id → [source names]
_has_embeddings: bool = False


def set_graph(graph: ProjectGraph, graph_json: dict) -> None:
    global _current_graph, _current_graph_json, _analysis_done, _analysis_error
    global _dep_index, _used_by_index, _has_embeddings
    _current_graph = graph
    _current_graph_json = graph_json
    _analysis_done = True
    _analysis_error = None

    unit_map = {u.id: u.name for u in graph.units}
    _dep_index = {}
    _used_by_index = {}
    for d in graph.dependencies:
        _dep_index.setdefault(d.source_id, []).append(
            unit_map.get(d.target_id, d.target_id.split("::")[-1])
        )
        _used_by_index.setdefault(d.target_id, []).append(
            unit_map.get(d.source_id, d.source_id.split("::")[-1])
        )
    _has_embeddings = any(u.embedding is not None for u in graph.units)


def set_error(msg: str) -> None:
    global _analysis_error, _analysis_done
    _analysis_error = msg
    _analysis_done = True


def is_ready() -> bool:
    return _analysis_done


class ExplainRequest(BaseModel):
    unit_id: str


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "ready": _analysis_done,
        "error": _analysis_error,
        "gpu_available": is_gpu_available(),
        "device": get_device_name(),
    }


@router.get("/graph")
async def get_graph():
    """Return the pre-analyzed graph. Returns 503 while analysis is still running."""
    if not _analysis_done:
        raise HTTPException(status_code=503, detail="Analysis in progress")
    if _analysis_error:
        raise HTTPException(status_code=500, detail=_analysis_error)
    return _current_graph_json


@router.post("/explain")
async def explain(req: ExplainRequest):
    if not _analysis_done:
        raise HTTPException(status_code=503, detail="Analysis in progress")
    if _current_graph is None:
        raise HTTPException(status_code=500, detail=_analysis_error or "No graph available")

    unit = next((u for u in _current_graph.units if u.id == req.unit_id), None)
    if unit is None:
        name = req.unit_id.split("::")[-1].split("/")[-1]
        return {
            "unit_id": req.unit_id,
            "summary": f"External dependency: {name}",
            "role": "Third-party package — not part of this codebase.",
            "depends_on": [],
            "used_by": [],
            "risk_notes": None,
        }

    dep_names    = _dep_index.get(req.unit_id, [])
    used_by_names = _used_by_index.get(req.unit_id, [])

    result = await explain_unit(unit, dep_names[:10], used_by_names[:10])
    return result.model_dump()


@router.get("/similar/{unit_id:path}")
async def similar(unit_id: str, top_k: int = 5):
    if not _analysis_done:
        raise HTTPException(status_code=503, detail="Analysis in progress")
    if _current_graph is None:
        raise HTTPException(status_code=500, detail="No graph available")

    if not _has_embeddings:
        raise HTTPException(status_code=400, detail="Embeddings not available")

    results = find_similar(_current_graph.units, unit_id, top_k=top_k)
    return {"unit_id": unit_id, "similar": [{"id": r[0], "score": r[1]} for r in results]}
