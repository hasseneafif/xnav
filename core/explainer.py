from __future__ import annotations
import logging

from .llm_client import LLMError, call_llm, parse_json_response
from .models import CodeUnit, ExplainResult

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
File: {file_path} | Type: {kind} | Name: {name}
Uses: {dependency_names} | Used by: {dependent_names}
Source: {source_snippet}

Respond ONLY with JSON: {{"summary": "one sentence max", "role": "3-5 word phrase", "depends_on": [], "used_by": [], "risk_notes": null}}
"summary" must be a single sentence. "role" must be 3-5 words. Be extremely concise.
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
