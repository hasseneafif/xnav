from __future__ import annotations
import ast
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import CodeUnit, Dependency

logger = logging.getLogger(__name__)

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    "target", "vendor", ".next", ".nuxt", ".svelte-kit",
    "out", "coverage", "bin", "obj", ".gradle", ".idea",
}

_SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".java", ".kt", ".rb", ".rs", ".cs", ".php",
    ".swift", ".vue", ".svelte",
}


# ─── Language configs ─────────────────────────────────────────────────────────

@dataclass
class LangConfig:
    name: str
    import_pats: list[re.Pattern] = field(default_factory=list)
    class_pats: list[re.Pattern] = field(default_factory=list)
    func_pats: list[re.Pattern] = field(default_factory=list)


import bisect

def _cp(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.MULTILINE) for p in patterns]


_JS_IMPORT_PATS = [
    r"""import\s+(?:type\s+)?(?:[\w*{}\s,]+\s+from\s+)?['"]([^'"]+)['"]""",
    r"""(?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)""",
]
_JS_CLASS_PATS = [
    r"""(?:^|[\s;])((?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+))""",
]
_JS_FUNC_PATS = [
    r"""(?:^|[\s;])(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*[\(<]""",
    r"""(?:^|[\s;])(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?(?:function\b|\()""",
    r"""(?:^|[\s;])(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\w+\s*=>""",
]

_CONFIGS: dict[str, LangConfig] = {}


def _reg(exts: list[str], cfg: LangConfig) -> None:
    for ext in exts:
        _CONFIGS[ext] = cfg


_reg([".js", ".jsx", ".mjs", ".cjs"], LangConfig(
    name="javascript",
    import_pats=_cp(_JS_IMPORT_PATS),
    class_pats=_cp(_JS_CLASS_PATS),
    func_pats=_cp(_JS_FUNC_PATS),
))

_reg([".ts", ".tsx"], LangConfig(
    name="typescript",
    import_pats=_cp(_JS_IMPORT_PATS),
    class_pats=_cp(_JS_CLASS_PATS + [
        r"""(?:export\s+)?interface\s+(\w+)[\s<{]""",
        r"""(?:export\s+)?(?:type)\s+(\w+)\s*[=<]""",
    ]),
    func_pats=_cp(_JS_FUNC_PATS),
))

_reg([".vue", ".svelte"], LangConfig(
    name="vue/svelte",
    import_pats=_cp(_JS_IMPORT_PATS),
    class_pats=_cp(_JS_CLASS_PATS),
    func_pats=_cp(_JS_FUNC_PATS),
))

_reg([".go"], LangConfig(
    name="go",
    import_pats=_cp([
        r'^import\s+"([^"]+)"',
        # paths inside import() blocks — only match lines that look like import paths
        r'^\s+"([a-zA-Z][^"]*(?:/[^"]+)*)"',
    ]),
    class_pats=_cp([
        r'^type\s+(\w+)\s+(?:struct|interface)\b',
    ]),
    func_pats=_cp([
        r'^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(',
    ]),
))

_reg([".java"], LangConfig(
    name="java",
    import_pats=_cp([
        r'^import\s+(?:static\s+)?([a-zA-Z][\w.]*\w)',
    ]),
    class_pats=_cp([
        r'(?:public\s+|private\s+|protected\s+|abstract\s+|final\s+)*(?:class|interface|enum|record|@interface)\s+(\w+)',
    ]),
    func_pats=_cp([
        r'(?:public|private|protected|static|final|synchronized|native|abstract|\s)+[\w<>\[\]?@]+\s+(\w+)\s*\([^;{]*\)\s*(?:throws\s+[\w,\s]+)?\{',
    ]),
))

_reg([".kt"], LangConfig(
    name="kotlin",
    import_pats=_cp([r'^import\s+([\w.]+)']),
    class_pats=_cp([
        r'(?:data\s+|sealed\s+|abstract\s+|open\s+)?(?:class|object|interface)\s+(\w+)',
        r'(?:sealed\s+)?interface\s+(\w+)',
    ]),
    func_pats=_cp([
        r'(?:override\s+)?(?:suspend\s+)?fun\s+(?:<[^>]+>\s+)?(\w+)\s*\(',
    ]),
))

_reg([".rb"], LangConfig(
    name="ruby",
    import_pats=_cp([
        r"require(?:_relative)?\s+['\"]([^'\"]+)['\"]",
    ]),
    class_pats=_cp([r'^(?:class|module)\s+([\w:]+)']),
    func_pats=_cp([r'^\s*def\s+(self\.)?(\w+)']),
))

_reg([".rs"], LangConfig(
    name="rust",
    import_pats=_cp([r'^use\s+([\w:]+(?:::\{[^}]+\})?)']),
    class_pats=_cp([
        r'^(?:pub(?:\([^)]+\))?\s+)?(?:struct|enum|trait)\s+(\w+)',
        r'^(?:pub(?:\([^)]+\))?\s+)?impl(?:<[^>]+>)?\s+(\w+)',
    ]),
    func_pats=_cp([
        r'^(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*\(',
    ]),
))

_reg([".cs"], LangConfig(
    name="csharp",
    import_pats=_cp([r'^using\s+([\w.]+)\s*;']),
    class_pats=_cp([
        r'(?:public|private|protected|internal|static|abstract|sealed|\s)+(?:class|interface|struct|enum|record)\s+(\w+)',
    ]),
    func_pats=_cp([
        r'(?:public|private|protected|internal|static|async|virtual|override|abstract|\s)+[\w<>\[\]?]+\s+(\w+)\s*\([^)]*\)\s*(?:where\s+[^{]+)?\{',
    ]),
))

_reg([".php"], LangConfig(
    name="php",
    import_pats=_cp([
        r"(?:require|include)(?:_once)?\s+['\"]([^'\"]+)['\"]",
        r'^use\s+([\w\\]+)',
    ]),
    class_pats=_cp([
        r'(?:abstract\s+)?(?:class|interface|trait|enum)\s+(\w+)',
    ]),
    func_pats=_cp([r'function\s+(\w+)\s*\(']),
))

_reg([".swift"], LangConfig(
    name="swift",
    import_pats=_cp([r'^import\s+(\w+)']),
    class_pats=_cp([
        r'(?:public\s+|private\s+|internal\s+|open\s+|final\s+)*(?:class|struct|protocol|enum|actor)\s+(\w+)',
    ]),
    func_pats=_cp([
        r'(?:public\s+|private\s+|internal\s+|open\s+)?(?:override\s+)?func\s+(\w+)\s*[\(<]',
    ]),
))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _unit_id(file_path: str, *names: str) -> str:
    return "::".join([file_path] + list(names))


def _source_snippet(lines: list[str], start: int, max_lines: int = 20) -> str:
    return "".join(lines[start - 1 : start - 1 + max_lines])


def _resolve_js_import(import_path: str, current_file: str, known_files: set[str]) -> str:
    """Resolve a relative JS/TS import path to a repo-relative path."""
    if not import_path.startswith("."):
        return import_path  # external package

    base_dir = "/".join(current_file.split("/")[:-1])
    parts = (base_dir + "/" + import_path).split("/")
    resolved: list[str] = []
    for part in parts:
        if part == "..":
            if resolved:
                resolved.pop()
        elif part and part != ".":
            resolved.append(part)
    resolved_path = "/".join(resolved)

    # Try to find exact match with common extensions
    for ext in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js"):
        candidate = resolved_path + ext
        if candidate in known_files:
            return candidate
    return resolved_path


# ─── Python parser (AST) ─────────────────────────────────────────────────────

def _parse_python_file(
    file_path: Path, repo_root: Path
) -> tuple[list[CodeUnit], list[Dependency]]:
    rel_path = str(file_path.relative_to(repo_root)).replace("\\", "/")
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("Cannot read %s: %s", rel_path, e)
        return [], []

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as e:
        logger.warning("Skipping %s (syntax error: %s)", rel_path, e)
        return [], []

    lines = source.splitlines(keepends=True)
    units: list[CodeUnit] = []
    deps: list[Dependency] = []

    module_id = _unit_id(rel_path)
    units.append(CodeUnit(
        id=module_id, name=file_path.stem, kind="module",
        file_path=rel_path, line_start=1, line_end=len(lines),
        docstring=ast.get_docstring(tree),
        source_snippet=_source_snippet(lines, 1),
    ))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                deps.append(Dependency(source_id=module_id, target_id=alias.name.replace(".", "/") + ".py", kind="imports"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level and node.level > 0:
                base = "/".join(rel_path.split("/")[:-node.level])
                target = base + "/" + node.module.replace(".", "/") + ".py"
            else:
                target = node.module.replace(".", "/") + ".py"
            deps.append(Dependency(source_id=module_id, target_id=target, kind="imports"))

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            class_id = _unit_id(rel_path, node.name)
            units.append(CodeUnit(
                id=class_id, name=node.name, kind="class",
                file_path=rel_path, line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                docstring=ast.get_docstring(node),
                source_snippet=_source_snippet(lines, node.lineno),
            ))
            deps.append(Dependency(source_id=module_id, target_id=class_id, kind="imports"))
            for base in node.bases:
                if isinstance(base, ast.Name):
                    deps.append(Dependency(source_id=class_id, target_id=base.id, kind="inherits"))
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_id = _unit_id(rel_path, node.name, child.name)
                    units.append(CodeUnit(
                        id=method_id, name=child.name, kind="method",
                        file_path=rel_path, line_start=child.lineno,
                        line_end=child.end_lineno or child.lineno,
                        docstring=ast.get_docstring(child),
                        source_snippet=_source_snippet(lines, child.lineno),
                    ))
                    deps.append(Dependency(source_id=class_id, target_id=method_id, kind="calls"))

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_id = _unit_id(rel_path, node.name)
            units.append(CodeUnit(
                id=func_id, name=node.name, kind="function",
                file_path=rel_path, line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                docstring=ast.get_docstring(node),
                source_snippet=_source_snippet(lines, node.lineno),
            ))
            deps.append(Dependency(source_id=module_id, target_id=func_id, kind="calls"))

    return units, deps


# ─── Generic regex parser ─────────────────────────────────────────────────────

def _first_group(m: re.Match) -> str:
    """Return the first non-None captured group from a match."""
    return next((g for g in m.groups() if g is not None), m.group(0))


def _parse_generic_file(
    file_path: Path, repo_root: Path, known_files: set[str], lang: LangConfig
) -> tuple[list[CodeUnit], list[Dependency]]:
    rel_path = str(file_path.relative_to(repo_root)).replace("\\", "/")
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("Cannot read %s: %s", rel_path, e)
        return [], []

    lines = source.splitlines(keepends=True)
    units: list[CodeUnit] = []
    deps: list[Dependency] = []

    module_id = _unit_id(rel_path)
    units.append(CodeUnit(
        id=module_id, name=file_path.stem, kind="module",
        file_path=rel_path, line_start=1, line_end=len(lines),
        source_snippet=_source_snippet(lines, 1),
    ))

    # Precompute line-start offsets for O(log n) lineno lookup instead of O(n) per match
    line_starts = [0]
    for i, ch in enumerate(source):
        if ch == '\n':
            line_starts.append(i + 1)

    def _lineno(offset: int) -> int:
        return bisect.bisect_right(line_starts, offset)

    # Imports / dependencies
    for pat in lang.import_pats:
        for m in pat.finditer(source):
            raw = _first_group(m)
            if not raw:
                continue
            if lang.name in ("javascript", "typescript", "vue/svelte"):
                target = _resolve_js_import(raw, rel_path, known_files)
            else:
                target = raw
            deps.append(Dependency(source_id=module_id, target_id=target, kind="imports"))

    # Classes
    seen_names: set[str] = set()
    for pat in lang.class_pats:
        for m in pat.finditer(source):
            name = _first_group(m).strip()
            if not name or name in seen_names or re.match(r'^(import|export|class|function|const|let|var)$', name):
                continue
            seen_names.add(name)
            lineno = _lineno(m.start())
            class_id = _unit_id(rel_path, name)
            units.append(CodeUnit(
                id=class_id, name=name, kind="class",
                file_path=rel_path, line_start=lineno, line_end=lineno,
                source_snippet=_source_snippet(lines, lineno),
            ))
            deps.append(Dependency(source_id=module_id, target_id=class_id, kind="imports"))

    # Functions
    for pat in lang.func_pats:
        for m in pat.finditer(source):
            name = _first_group(m).strip()
            if not name or name in seen_names or re.match(r'^(if|for|while|switch|catch|function)$', name):
                continue
            seen_names.add(name)
            lineno = _lineno(m.start())
            func_id = _unit_id(rel_path, name)
            units.append(CodeUnit(
                id=func_id, name=name, kind="function",
                file_path=rel_path, line_start=lineno, line_end=lineno,
                source_snippet=_source_snippet(lines, lineno),
            ))
            deps.append(Dependency(source_id=module_id, target_id=func_id, kind="calls"))

    return units, deps


# ─── Directory walker ─────────────────────────────────────────────────────────

def parse_directory(repo_root: str | Path) -> tuple[list[CodeUnit], list[Dependency]]:
    """Walk a directory, parse all supported source files, return units + dependencies."""
    root = Path(repo_root)
    all_units: list[CodeUnit] = []
    all_deps: list[Dependency] = []

    # First pass: collect all file paths (needed for JS import resolution)
    all_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            fp = Path(dirpath) / fname
            if fp.suffix.lower() in _SUPPORTED_EXTENSIONS:
                all_files.append(fp)

    known_files: set[str] = {
        str(fp.relative_to(root)).replace("\\", "/") for fp in all_files
    }

    # Count by extension for logging
    from collections import Counter
    ext_counts = Counter(fp.suffix.lower() for fp in all_files)
    logger.info("Found %d supported files: %s", len(all_files),
                ", ".join(f"{v} {k}" for k, v in ext_counts.most_common(8)))

    if not all_files:
        exts_found = Counter(
            Path(dirpath, fname).suffix.lower()
            for dirpath, _, filenames in os.walk(root)
            for fname in filenames
        )
        top = ", ".join(f"{k}({v})" for k, v in exts_found.most_common(10))
        raise ValueError(
            f"No supported source files found in {root}.\n"
            f"Extensions present: {top or 'none'}\n"
            f"Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )

    # Second pass: parse
    for fp in all_files:
        ext = fp.suffix.lower()
        if ext == ".py":
            units, deps = _parse_python_file(fp, root)
        else:
            lang = _CONFIGS.get(ext)
            if lang is None:
                continue
            units, deps = _parse_generic_file(fp, root, known_files, lang)
        all_units.extend(units)
        all_deps.extend(deps)

    logger.info("Parsed %d units and %d dependencies from %s",
                len(all_units), len(all_deps), root)
    return all_units, all_deps
