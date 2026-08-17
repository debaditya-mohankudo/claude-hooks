"""Resolves a concept_store evidence citation ("file:symbol") to its current line range.

Citations store a symbol name only, never a line number (see store.py's evidence field
docs) — a name encodes no position, so it can't drift the way a line range does. This
module is the on-demand lookup that turns a citation back into a line range for anything
that wants to jump to or display the cited code.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional


class SymbolNotFoundError(ValueError):
    """Raised when a citation's symbol can't be located in its file."""


def parse_citation(citation: str) -> tuple[str, Optional[str]]:
    """Splits "file:symbol" into (file, symbol); a bare "file" (no colon) returns (file, None)."""
    file, sep, symbol = citation.partition(":")
    return file, (symbol if sep else None)


def resolve_symbol(repo_root: Path, file: str, symbol: str) -> tuple[int, int]:
    """Returns the 1-indexed inclusive (start_line, end_line) of `symbol` in `file`.

    `symbol` is either a top-level name ("build_agents") or "ClassName.method_name"
    for a method nested one level inside a class.
    """
    source = (Path(repo_root) / file).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=file)
    parts = symbol.split(".")

    def find(nodes: list[ast.stmt], name: str) -> Optional[ast.AST]:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                return node
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                return node
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                return node
        return None

    node = find(tree.body, parts[0])
    if node is None:
        raise SymbolNotFoundError(f"{parts[0]!r} not found at module level of {file}")
    for part in parts[1:]:
        if not isinstance(node, ast.ClassDef):
            raise SymbolNotFoundError(f"{'.'.join(parts)!r}: {node.name!r} is not a class, cannot descend to {part!r}")
        child = find(node.body, part)
        if child is None:
            raise SymbolNotFoundError(f"{part!r} not found in class {node.name!r} of {file}")
        node = child

    return node.lineno, getattr(node, "end_lineno", node.lineno)
