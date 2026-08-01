#!/usr/bin/env python3
"""Build a code graph for claude-hooks and write .code_graph.json.

The graph captures:
  - imports: which modules each file imports (project-local only)
  - symbols: classes and top-level functions defined in each file
  - callers: which files reference each symbol (reverse index)

Output is written to <repo_root>/.code_graph.json — gitignored.

Usage:
    uv run python scripts/build_code_graph.py
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules"}
OUT_FILE = REPO_ROOT / ".code_graph.json"


def _module_key(path: Path) -> str:
    """Convert absolute path to dotted module key, e.g. langchain_learning.nodes.load_turn."""
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _collect_files() -> list[Path]:
    files = []
    for p in sorted(REPO_ROOT.rglob("*.py")):
        if any(skip in p.parts for skip in SKIP_DIRS):
            continue
        files.append(p)
    return files


def _parse_file(path: Path) -> dict:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return {"imports": [], "symbols": [], "raw_imports": []}

    local_imports: list[str] = []
    raw_imports: list[str] = []
    symbols: list[dict] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                raw_imports.append(alias.name)
                if _is_local(alias.name):
                    local_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            raw_imports.append(mod)
            if _is_local(mod):
                local_imports.append(mod)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({"name": node.name, "kind": "function", "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            methods = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append({"name": child.name, "line": child.lineno})
            symbols.append({"name": node.name, "kind": "class", "line": node.lineno, "methods": methods})

    return {
        "imports": sorted(set(local_imports)),
        "raw_imports": sorted(set(raw_imports)),
        "symbols": symbols,
    }


_LOCAL_ROOTS = {"langchain_learning", "hooks", "src", "scripts", "tests"}


def _is_local(module: str) -> bool:
    if not module:
        return False
    root = module.split(".")[0]
    return root in _LOCAL_ROOTS


def build_graph() -> dict:
    files = _collect_files()
    graph: dict[str, dict] = {}

    for path in files:
        key = _module_key(path)
        rel = str(path.relative_to(REPO_ROOT))
        info = _parse_file(path)
        graph[key] = {
            "file": rel,
            "imports": info["imports"],
            "symbols": info["symbols"],
        }

    # reverse index: symbol_name → list of modules that define it
    symbol_index: dict[str, list[str]] = {}
    for mod, data in graph.items():
        for sym in data["symbols"]:
            symbol_index.setdefault(sym["name"], []).append(mod)

    # reverse import index: mod → list of modules that import it
    imported_by: dict[str, list[str]] = {}
    for mod, data in graph.items():
        for imp in data["imports"]:
            imported_by.setdefault(imp, []).append(mod)

    return {
        "modules": graph,
        "symbol_index": symbol_index,
        "imported_by": imported_by,
    }


def _git_meta() -> dict:
    import subprocess
    from datetime import datetime, timezone
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
        short = sha[:12]
    except Exception:
        sha = short = "unknown"
    return {
        "commit": sha,
        "commit_short": short,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    print(f"Scanning {REPO_ROOT} ...")
    g = build_graph()
    g["meta"] = _git_meta()
    n_modules = len(g["modules"])
    n_symbols = sum(len(v) for v in g["symbol_index"].values())
    OUT_FILE.write_text(json.dumps(g, indent=2), encoding="utf-8")
    print(f"Written {OUT_FILE.relative_to(REPO_ROOT)}")
    print(f"  {n_modules} modules, {n_symbols} symbols")
    print(f"  commit: {g['meta']['commit_short']} ({g['meta']['generated_at']})")


if __name__ == "__main__":
    main()
