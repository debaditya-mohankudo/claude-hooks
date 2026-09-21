"""Group-level tool routing over ontology/mcp-tools-domain.json.

ScoreToolsNode returns a flat top-N of individual tools. This lifts those hits
to their tool group: which groups the hits fall in, each group's category, its
sibling tools, and the groups that overlap it (interchangeable alternatives).
Pure and fail-soft -- a missing/unreadable graph yields no routing, never an error.

Tags: tool-hints, tool-graph, routing, ontology
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from src.logger import get_logger

_log = get_logger(__name__)

GRAPH_PATH = Path(__file__).resolve().parents[1] / "ontology" / "mcp-tools-domain.json"
_TOKEN = re.compile(r"[\w-]+\*?")


@lru_cache(maxsize=4)
def load_graph(path: Path = GRAPH_PATH) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        _log.warning("[tool_groups] graph unavailable: %s", exc)
        return None


def _index(graph: dict) -> tuple[dict, dict, dict]:
    """(groups by id, overlap alternatives by group id, group-name -> [group ids])."""
    groups = {n["id"]: n for n in graph["nodes"] if n["kind"] == "tool_group"}
    alts: dict[str, list[str]] = {gid: [] for gid in groups}
    for e in graph["edges"]:
        if e["relation"] == "overlaps_with":
            alts.setdefault(e["from"], []).append(e["to"])
            alts.setdefault(e["to"], []).append(e["from"])
    by_name: dict[str, list[str]] = {}
    for gid in groups:
        by_name.setdefault(gid.split(":", 2)[2], []).append(gid)
    return groups, alts, by_name


def group_of(tool_name: str, groups: dict, by_name: dict) -> str | None:
    """Map a hint's short name (`vault__write`, `update_run`) to a group id, or None.

    `grp__tool` resolves by group name; ambiguous names (code_rag lives in two
    servers) are skipped. Bare names (wyckoff-analyzer tools) match a group's
    listed tools, including `prefix_*` wildcards.
    """
    if "__" in tool_name:
        cands = by_name.get(tool_name.split("__", 1)[0], [])
        return cands[0] if len(cands) == 1 else None
    for gid, node in groups.items():
        for tok in _TOKEN.findall(node.get("tools", "")):
            if tok == tool_name or (tok.endswith("_*") and tool_name.startswith(tok[:-1])):
                return gid
    return None


def route_groups(hints: list[dict], graph: dict | None = None, top_groups: int = 3) -> list[dict]:
    """Rank the groups the hinted tools fall in. `hints` is ScoreToolsNode output
    (already best-first); groups rank by hit count, then by best hint position."""
    graph = graph if graph is not None else load_graph()
    if not graph or not hints:
        return []
    groups, alts, by_name = _index(graph)
    hit: dict[str, list[str]] = {}
    first: dict[str, int] = {}
    for i, h in enumerate(hints):
        gid = group_of(h.get("tool_name", ""), groups, by_name)
        if gid:
            hit.setdefault(gid, []).append(h["tool_name"])
            first.setdefault(gid, i)
    ranked = sorted(hit, key=lambda g: (-len(hit[g]), first[g]))[:top_groups]
    return [{
        "group": groups[g]["label"],
        "category": groups[g].get("category", ""),
        "matched": hit[g],
        "tools": groups[g].get("tools", ""),
        "alternatives": [groups[a]["label"] for a in alts.get(g, []) if a in groups],
    } for g in ranked]


def format_groups(routes: list[dict], max_tools: int = 110) -> list[str]:
    """Prompt lines for the `## Suggested tool groups` section (empty if no routes)."""
    if not routes:
        return []
    lines = ["## Suggested tool groups"]
    for r in routes:
        tools = r["tools"] if len(r["tools"]) <= max_tools else r["tools"][:max_tools].rstrip(", ") + "..."
        line = f"- `{r['group']}` [{r['category']}] — hit: {', '.join(r['matched'])}; group has: {tools}"
        if r["alternatives"]:
            line += f"; overlaps: {', '.join(r['alternatives'])}"
        lines.append(line)
    lines.append("")
    return lines
