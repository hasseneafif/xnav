from __future__ import annotations
import logging
import os
import time
from pathlib import Path

from .classifier import classify_clusters
from .embedder import embed_units, get_device_name, is_gpu_available
from .graph import build_graph, to_json
from .models import AnalysisStats, ProjectGraph
from .parser import parse_directory

logger = logging.getLogger(__name__)


async def analyze_repo(source: str) -> tuple[ProjectGraph, dict]:
    """
    Full analysis pipeline for a local path.
    Returns (ProjectGraph, graph_json_dict).
    """
    embed_model = os.getenv("XNAV_EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")

    local_path = Path(source).resolve()
    if not local_path.exists():
        raise ValueError(f"Path does not exist: {local_path}")

    repo_name = local_path.name

    parse_start = time.perf_counter()
    units, deps = parse_directory(local_path)
    parse_ms = (time.perf_counter() - parse_start) * 1000
    logger.info("Parse complete: %d units, %d deps in %.1fms", len(units), len(deps), parse_ms)

    units, embed_ms = embed_units(units, model_name=embed_model)

    cluster_start = time.perf_counter()
    cluster_map = await classify_clusters(units, deps, local_path)
    cluster_ms = (time.perf_counter() - cluster_start) * 1000
    for u in units:
        if u.kind == "module":
            u.cluster = cluster_map.get(u.file_path)
    module_cluster_by_path = {u.file_path: u.cluster for u in units if u.kind == "module"}
    for u in units:
        if u.kind != "module":
            u.cluster = module_cluster_by_path.get(u.file_path)
    logger.info("Classified clusters in %.1fms (%d distinct)",
                cluster_ms, len({c for c in cluster_map.values()}))

    stats = AnalysisStats(
        total_files=len({u.file_path for u in units if u.kind == "module"}),
        total_units=len(units),
        total_dependencies=len(deps),
        parse_time_ms=parse_ms,
        embed_time_ms=embed_ms,
        gpu_available=is_gpu_available(),
        device_name=get_device_name(),
    )
    g, project_graph = build_graph(
        units, deps,
        stats=stats,
        repo_name=repo_name,
        repo_path=str(local_path),
    )
    graph_json = to_json(g, project_graph)
    return project_graph, graph_json
