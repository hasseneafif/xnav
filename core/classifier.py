from __future__ import annotations
import datetime as dt
import hashlib
import json
import logging
import re
from collections import Counter
from pathlib import Path

from . import llm_client
from .llm_client import LLMError
from .models import CodeUnit, Dependency

logger = logging.getLogger(__name__)

CACHE_DIR_NAME = ".xnav"
CACHE_FILE_NAME = "clusters.json"
CACHE_VERSION = 1
MAX_CLUSTERS = 8

SYSTEM_PROMPT = (
    "You are a senior software architect classifying source files into 4–6 high-level "
    "semantic clusters so a developer can understand the project at a glance. "
    "Use these names where they fit (you may add others if truly needed): "
    "Frontend, CLI, Backend, API, Database, Models, Auth, Config, Utilities, Tests, External, Build. "
    "Important: use 'Frontend' for all UI/web/client-side code; use 'CLI' for command-line entry points. "
    "Keep names SHORT — 1-2 words max, ≤ 12 chars, Title Case, no emoji. Aim for 4–6 clusters max — aggressively merge related files. Never create a cluster for a single file. "
    'Output ONLY a JSON object mapping each input file path to a cluster name. No prose, no markdown.'
)

# ─── Path heuristics ──────────────────────────────────────────────────────────

_HEURISTIC_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(^|/)(tests?|spec)(/|$)|_test\.|\.test\.|\.spec\."),       "Tests"),
    (re.compile(r"(^|/)(frontend|web|client)(/|$)|\.(tsx|jsx|vue|svelte)$"), "Frontend"),
    (re.compile(r"(^|/)(ui|components|views|pages)(/|$)"),                    "Frontend"),
    (re.compile(r"(^|/)cli(\.py)?$|/cli/"),                                   "CLI"),
    (re.compile(r"(^|/)(api|routes|controllers|endpoints|handlers)(/|$)"),    "API"),
    (re.compile(r"(^|/)(auth|security)(/|$)|oauth|jwt|login"),                "Auth"),
    (re.compile(r"(^|/)(db|database|models|orm|schema|migrations)(/|$)"),     "Database"),
    (re.compile(r"(^|/)(config|settings)(/|$)|\.env"),                        "Config"),
    (re.compile(r"(^|/)(utils|helpers|common|lib|shared)(/|$)"),              "Utilities"),
    (re.compile(r"\.(yml|yaml|toml|ini)$|dockerfile|makefile"),               "Build"),
    (re.compile(r"(^|/)(server|backend|core|services|business)(/|$)"),        "Backend"),
]

# Aliases the LLM might use — normalize to canonical names
_ALIASES: dict[str, str] = {
    "frontend": "Frontend", "client": "Frontend", "web": "Frontend",
    "backend": "Backend",   "server": "Backend",
    "api": "API", "routes": "API", "controllers": "API",
    "db": "Database", "database": "Database", "orm": "Database", "schema": "Database",
    "auth": "Auth", "security": "Auth", "login": "Auth",
    "util": "Utilities", "utils": "Utilities", "helpers": "Utilities", "common": "Utilities",
    "test": "Tests", "tests": "Tests", "spec": "Tests",
    "config": "Config", "settings": "Config", "env": "Config",
    "build": "Build", "ci": "Build", "deploy": "Build",
    "ui": "Frontend", "components": "Frontend", "views": "Frontend",
    "cli": "CLI", "command line": "CLI", "cmd": "CLI",
    "models": "Models", "model": "Models",
    "external": "External", "third-party": "External", "vendor": "External",
}


def _heuristic_for_path(path: str) -> str:
    p = path.lower().replace("\\", "/")
    for pat, cluster in _HEURISTIC_RULES:
        if pat.search(p):
            return cluster
    parts = p.split("/")
    if len(parts) > 1 and parts[0]:
        return parts[0].title().replace("_", " ").replace("-", " ")
    return "Root"


def _path_heuristic(module_units: list[CodeUnit]) -> dict[str, str]:
    return {u.file_path: _heuristic_for_path(u.file_path) for u in module_units}


def _canonicalize(name: str) -> str:
    if not name or not isinstance(name, str):
        return "Utilities"
    cleaned = re.sub(r"\s+", " ", name.strip())
    if not cleaned:
        return "Utilities"
    key = cleaned.lower()
    if key in _ALIASES:
        return _ALIASES[key]
    if cleaned == cleaned.lower() or cleaned == cleaned.upper():
        cleaned = cleaned.title()
    if len(cleaned) > 12:
        words = cleaned.split()
        result = words[0]
        for w in words[1:]:
            if len(result) + 1 + len(w) <= 12:
                result += " " + w
            else:
                break
        cleaned = result
    return cleaned


def _cap_clusters(mapping: dict[str, str], max_clusters: int = MAX_CLUSTERS) -> dict[str, str]:
    """Merge config/build singletons into larger clusters, then cap total count."""
    _MERGEABLE = {"Config", "Build", "Utilities", "External", "Root"}

    counts = Counter(mapping.values())
    large = {name for name, cnt in counts.items() if cnt >= 2}

    if large:
        result: dict[str, str] = {}
        for path, cluster in mapping.items():
            if counts[cluster] < 2 and cluster in _MERGEABLE:
                heuristic = _heuristic_for_path(path)
                result[path] = heuristic if heuristic in large else max(large, key=lambda c: counts[c])
            else:
                result[path] = cluster
        mapping = result
        counts = Counter(mapping.values())

    if len(counts) <= max_clusters:
        return mapping
    keep = {name for name, _ in counts.most_common(max_clusters - 1)}
    keep.add("Utilities")
    return {p: (c if c in keep else "Utilities") for p, c in mapping.items()}


# ─── Cache ────────────────────────────────────────────────────────────────────

def _signature(file_paths: list[str]) -> str:
    h = hashlib.sha1()
    for p in sorted(file_paths):
        h.update(p.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def _cache_path(repo_path: Path) -> Path:
    return repo_path / CACHE_DIR_NAME / CACHE_FILE_NAME


def _load_cache(repo_path: Path, signature: str) -> dict[str, str] | None:
    path = _cache_path(repo_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != CACHE_VERSION or data.get("signature") != signature:
            return None
        mapping = data.get("mapping")
        if isinstance(mapping, dict):
            logger.info("Cluster cache hit (%s, %s)", data.get("method"), data.get("model", "n/a"))
            return mapping
    except Exception as e:
        logger.warning("Failed to read cluster cache: %s", e)
    return None


def _write_cache(repo_path: Path, signature: str, mapping: dict[str, str], method: str) -> None:
    try:
        cache_dir = repo_path / CACHE_DIR_NAME
        cache_dir.mkdir(exist_ok=True)
        data = {
            "version": CACHE_VERSION,
            "signature": signature,
            "generated_at": dt.datetime.utcnow().isoformat() + "Z",
            "model": llm_client.LLM_MODEL if method == "llm" else None,
            "method": method,
            "mapping": mapping,
        }
        (cache_dir / CACHE_FILE_NAME).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to write cluster cache: %s", e)


# ─── LLM input building ──────────────────────────────────────────────────────

def _build_module_lines(module_units: list[CodeUnit], dependencies: list[Dependency], all_units: list[CodeUnit]) -> dict[str, str]:
    """One descriptive line per module file for the LLM prompt."""
    deps_by_source: dict[str, list[str]] = {}
    for d in dependencies:
        if d.kind == "imports":
            deps_by_source.setdefault(d.source_id, []).append(d.target_id.split("::")[-1].split("/")[-1])

    children_by_path: dict[str, list[str]] = {}
    for u in all_units:
        if u.kind in ("class", "function", "method"):
            children_by_path.setdefault(u.file_path, []).append(u.name)

    lines: dict[str, str] = {}
    for u in module_units:
        imports = list(dict.fromkeys(deps_by_source.get(u.id, [])))[:8]
        defines = children_by_path.get(u.file_path, [])[:6]
        parts = [u.file_path]
        if imports:
            parts.append(f"imports: {', '.join(imports)}")
        if defines:
            parts.append(f"defines: {', '.join(defines)}")
        lines[u.file_path] = " | ".join(parts)
    return lines


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _llm_classify(module_lines: dict[str, str], repo_name: str) -> dict[str, str]:
    """Call the LLM in one or more batches and return {file_path: cluster}."""
    paths = list(module_lines.keys())
    result: dict[str, str] = {}

    for batch in _chunks(paths, 150):
        body_lines = "\n".join(module_lines[p] for p in batch)
        prompt = (
            f"Project: {repo_name}\n"
            f"Files ({len(batch)}):\n{body_lines}\n\n"
            'Return JSON: {"<file_path>": "<cluster>", ...}'
        )
        raw = await llm_client.call_llm(prompt, system=SYSTEM_PROMPT, timeout=120, max_tokens=4000)
        data = llm_client.parse_json_response(raw)
        if not isinstance(data, dict):
            raise LLMError("LLM did not return a JSON object")
        for path, cluster in data.items():
            if isinstance(cluster, str):
                result[path] = _canonicalize(cluster)
    return result


# ─── Public API ───────────────────────────────────────────────────────────────

async def classify_clusters(
    units: list[CodeUnit],
    dependencies: list[Dependency],
    repo_path: Path,
    *,
    use_cache: bool = True,
) -> dict[str, str]:
    """Returns {file_path -> cluster_name} for every module file."""
    module_units = [u for u in units if u.kind == "module"]
    if not module_units:
        return {}

    paths = [u.file_path for u in module_units]
    sig = _signature(paths)

    if use_cache:
        cached = _load_cache(repo_path, sig)
        if cached is not None:
            return cached

    # No-LLM short-circuit
    if not llm_client.is_configured():
        logger.info("LLM not configured — using path heuristic for clustering")
        mapping = _path_heuristic(module_units)
        mapping = _cap_clusters(mapping)
        _write_cache(repo_path, sig, mapping, method="heuristic")
        return mapping

    module_lines = _build_module_lines(module_units, dependencies, units)
    repo_name = repo_path.name
    method = "llm"

    try:
        llm_mapping = await _llm_classify(module_lines, repo_name)
    except LLMError as e:
        logger.warning("LLM classification failed (%s) — falling back to heuristic", e)
        llm_mapping = {}
        method = "heuristic"
    except Exception as e:
        logger.warning("Cluster classification error (%s) — falling back to heuristic", e)
        llm_mapping = {}
        method = "heuristic"

    # Fill any missing files with heuristic
    heuristic_mapping = _path_heuristic(module_units)
    final_mapping: dict[str, str] = {}
    for path in paths:
        if path in llm_mapping and llm_mapping[path]:
            final_mapping[path] = llm_mapping[path]
        else:
            final_mapping[path] = heuristic_mapping[path]

    final_mapping = _cap_clusters(final_mapping)
    _write_cache(repo_path, sig, final_mapping, method=method)
    logger.info("Classified %d files into %d clusters via %s",
                len(final_mapping), len(set(final_mapping.values())), method)
    return final_mapping
