from __future__ import annotations
from typing import Literal
from pydantic import BaseModel


class CodeUnit(BaseModel):
    """A single analyzable unit — function, class, or module."""
    id: str
    name: str
    kind: Literal["module", "class", "function", "method"]
    file_path: str
    line_start: int
    line_end: int
    docstring: str | None = None
    source_snippet: str = ""
    embedding: list[float] | None = None
    cluster: str | None = None


class Dependency(BaseModel):
    """A directed edge: source depends on target."""
    source_id: str
    target_id: str
    kind: Literal["imports", "calls", "inherits"]


class AnalysisStats(BaseModel):
    """Timing and size info for the analysis run."""
    total_files: int
    total_units: int
    total_dependencies: int
    parse_time_ms: float
    embed_time_ms: float
    gpu_available: bool
    device_name: str


class ProjectGraph(BaseModel):
    """The complete analysis result."""
    repo_name: str
    repo_path: str
    units: list[CodeUnit]
    dependencies: list[Dependency]
    stats: AnalysisStats


class ExplainResult(BaseModel):
    """LLM-generated explanation of a code unit."""
    unit_id: str
    summary: str
    role: str
    depends_on: list[str]
    used_by: list[str]
    risk_notes: str | None = None
