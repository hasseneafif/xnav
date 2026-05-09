from __future__ import annotations
import logging

from .llm_client import LLMError, call_llm, parse_json_response
from .models import CodeUnit, ExplainResult

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
File: {file_path} | Type: {kind} | Name: {name}
Uses: {dependency_names} | Used by: {dependent_names}
Source: {source_snippet}

Write 1-2 sentences for "summary" explaining what this code does and why it exists.
For "risk_notes", write a short observation about this code — notable patterns, design decisions, performance considerations, coupling, or potential issues. Always try to provide something useful here.
"role" must be a 3-5 word phrase describing the responsibility.

Respond ONLY with JSON: {{"summary": "...", "role": "...", "depends_on": [], "used_by": [], "risk_notes": null}}
"""


def _build_prompt(unit: CodeUnit, dep_names: list[str], used_by_names: list[str]) -> str:
    return _PROMPT_TEMPLATE.format(
        file_path=unit.file_path,
        kind=unit.kind,
        name=unit.name,
        source_snippet=unit.source_snippet[:800],
        dependency_names=", ".join(dep_names) or "none",
        dependent_names=", ".join(used_by_names) or "none",
    )


def _fallback(unit: CodeUnit, dep_names: list[str], used_by_names: list[str]) -> ExplainResult:
    return ExplainResult(
        unit_id=unit.id,
        summary=unit.docstring or f"{unit.kind} '{unit.name}' — no docstring available.",
        role=f"Located in {unit.file_path}",
        depends_on=dep_names,
        used_by=used_by_names,
        risk_notes=None,
    )


async def explain_unit(
    unit: CodeUnit,
    dep_names: list[str],
    used_by_names: list[str],
) -> ExplainResult:
    """Call the configured LLM to explain a code unit. Falls back to docstring on any failure."""
    prompt = _build_prompt(unit, dep_names, used_by_names)

    try:
        raw = await call_llm(prompt)
    except LLMError as e:
        logger.warning("LLM call failed for %s: %s", unit.id, e)
        return _fallback(unit, dep_names, used_by_names)

    try:
        data = parse_json_response(raw)
        return ExplainResult(
            unit_id=unit.id,
            summary=data.get("summary", ""),
            role=data.get("role", ""),
            depends_on=data.get("depends_on", []),
            used_by=data.get("used_by", []),
            risk_notes=data.get("risk_notes"),
        )
    except Exception as e:
        logger.warning("Failed to parse LLM response for %s: %s", unit.id, e)
        return _fallback(unit, dep_names, used_by_names)
