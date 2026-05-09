from __future__ import annotations
import logging
from typing import Any

import networkx as nx

from .models import AnalysisStats, CodeUnit, Dependency, ProjectGraph

logger = logging.getLogger(__name__)


def build_graph(
    units: list[CodeUnit],
    dependencies: list[Dependency],
    stats: AnalysisStats | None = None,
    repo_name: str = "",
    repo_path: str = "",
) -> tuple[nx.DiGraph, ProjectGraph]:
    """Build a NetworkX DiGraph and a serializable ProjectGraph from parsed units."""
    g: nx.DiGraph = nx.DiGraph()

    unit_ids = {u.id for u in units}

    for unit in units:
        g.add_node(unit.id, **unit.model_dump(exclude={"embedding"}))

    for dep in dependencies:
        if dep.source_id in unit_ids or dep.target_id in unit_ids:
            g.add_edge(dep.source_id, dep.target_id, kind=dep.kind)

    if stats is None:
        stats = AnalysisStats(
            total_files=len({u.file_path for u in units if u.kind == "module"}),
            total_units=len(units),
            total_dependencies=len(dependencies),
            parse_time_ms=0.0,
            embed_time_ms=0.0,
            gpu_available=False,
            device_name="cpu",
        )

    project_graph = ProjectGraph(
        repo_name=repo_name,
        repo_path=repo_path,
        units=units,
        dependencies=dependencies,
        stats=stats,
    )

    return g, project_graph


def get_dependents(g: nx.DiGraph, unit_id: str) -> list[str]:
    """Return all nodes that have an edge pointing TO unit_id."""
    return list(g.predecessors(unit_id))


def get_dependencies(g: nx.DiGraph, unit_id: str) -> list[str]:
    """Return all nodes that unit_id has an edge pointing to."""
    return list(g.successors(unit_id))


def get_blast_radius(g: nx.DiGraph, unit_id: str, depth: int = 2) -> list[str]:
    """Return transitive dependents up to `depth` hops (nodes that would break if unit_id changes)."""
    visited: set[str] = set()
    frontier = {unit_id}
    for _ in range(depth):
        next_frontier: set[str] = set()
        for node in frontier:
            for pred in g.predecessors(node):
                if pred not in visited and pred != unit_id:
                    next_frontier.add(pred)
        visited.update(next_frontier)
        frontier = next_frontier
    return list(visited)


def get_clusters(g: nx.DiGraph) -> list[list[str]]:
    """Return community clusters using greedy modularity on the undirected projection."""
    undirected = g.to_undirected()
    if undirected.number_of_nodes() == 0:
        return []
    try:
        communities = nx.community.greedy_modularity_communities(undirected)
        return [sorted(c) for c in communities]
    except Exception as e:
        logger.warning("Community detection failed: %s", e)
        return []


def _group_from_path(file_path: str) -> str:
    """Derive a group name from the top-level directory of the file path."""
    parts = file_path.replace("\\", "/").split("/")
    return parts[0] if len(parts) > 1 else "root"


def to_json(g: nx.DiGraph, project_graph: ProjectGraph) -> dict[str, Any]:
    """Serialize the graph to the frontend-expected JSON format."""
    unit_map = {u.id: u for u in project_graph.units}
    node_cluster_by_id: dict[str, str] = {}

    nodes = []
    for node_id in g.nodes():
        unit = unit_map.get(node_id)
        if unit:
            cluster = unit.cluster or _group_from_path(unit.file_path)
            node_cluster_by_id[node_id] = cluster
            nodes.append({
                "id": unit.id,
                "name": unit.name,
                "kind": unit.kind,
                "file_path": unit.file_path,
                "line_start": unit.line_start,
                "line_end": unit.line_end,
                "group": _group_from_path(unit.file_path),
                "cluster": cluster,
                "has_embedding": unit.embedding is not None,
                "source_snippet": unit.source_snippet[:800] if unit.source_snippet else "",
                "docstring": unit.docstring or "",
            })
        else:
            node_cluster_by_id[node_id] = "External"
            nodes.append({
                "id": node_id,
                "name": node_id.split("::")[-1],
                "kind": "module",
                "file_path": node_id,
                "line_start": 0,
                "line_end": 0,
                "group": "external",
                "cluster": "External",
                "has_embedding": False,
            })

    edges = [
        {"source": src, "target": tgt, "kind": data.get("kind", "imports")}
        for src, tgt, data in g.edges(data=True)
    ]

    # Cluster summary: name, size (modules), unit_count (all kinds), top files by degree
    module_units_by_cluster: dict[str, list[str]] = {}
    unit_count_by_cluster: dict[str, int] = {}
    for unit in project_graph.units:
        c = unit.cluster or _group_from_path(unit.file_path)
        unit_count_by_cluster[c] = unit_count_by_cluster.get(c, 0) + 1
        if unit.kind == "module":
            module_units_by_cluster.setdefault(c, []).append(unit.file_path)
    # Add External cluster if any external nodes exist
    external_count = sum(1 for nid, c in node_cluster_by_id.items() if c == "External")
    if external_count and "External" not in unit_count_by_cluster:
        unit_count_by_cluster["External"] = external_count

    # Top files in each cluster ranked by degree in the dependency graph
    degree = dict(g.degree())
    clusters = []
    for name, files in module_units_by_cluster.items():
        files_sorted = sorted(files, key=lambda fp: -degree.get(fp, 0))
        clusters.append({
            "name": name,
            "size": len(files),
            "unit_count": unit_count_by_cluster.get(name, len(files)),
            "files": files_sorted[:5],
        })
    if external_count:
        clusters.append({
            "name": "External",
            "size": 0,
            "unit_count": external_count,
            "files": [],
        })

    # Cross-cluster edges (aggregated counts)
    cross_counts: dict[tuple[str, str], int] = {}
    for src, tgt in g.edges():
        sc = node_cluster_by_id.get(src)
        tc = node_cluster_by_id.get(tgt)
        if sc and tc and sc != tc:
            key = (sc, tc)
            cross_counts[key] = cross_counts.get(key, 0) + 1
    cluster_edges = [
        {"source": s, "target": t, "count": n}
        for (s, t), n in cross_counts.items()
    ]

    stats = project_graph.stats.model_dump()
    stats["repo_path"] = project_graph.repo_path
    stats["repo_name"] = project_graph.repo_name
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": stats,
        "clusters": clusters,
        "cluster_edges": cluster_edges,
    }
