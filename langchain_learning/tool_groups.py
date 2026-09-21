"""Group-level tool routing over ~/.claude/mcp-tools-domain.json.

ScoreToolsNode returns a flat top-N of individual tools. This lifts those hits
to their tool group: which groups the hits fall in, each group's category, its
sibling tools, and the groups that overlap it (interchangeable alternatives).
Pure and fail-soft -- a missing/unreadable graph yields no routing, never an error.

Tags: tool-hints, tool-graph, routing, ontology
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.logger import get_logger

_log = get_logger(__name__)

GRAPH_PATH = Path.home() / ".claude" / "mcp-tools-domain.json"  # personal: names what the user works on, so not in the public repo
_TOKEN = re.compile(r"[\w-]+\*?")


# (path, mtime_ns) -> graph. Only successful loads are cached, keyed on mtime, so a
# missing file at first use recovers once it appears and edits are picked up without
# a server restart (task:750242a5 grooming: lru_cache pinned a failed load as None).
_cache: dict[tuple[Path, int], dict] = {}


def load_graph(path: Path = GRAPH_PATH) -> dict | None:
    try:
        key = (path, path.stat().st_mtime_ns)
        if key not in _cache:
            _cache.clear()
            _cache[key] = json.loads(path.read_text())
        return _cache[key]
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


def group_keywords(node: dict) -> set[str]:
    """A group's curated keywords as prompt-comparable tokens.

    Run through the same tokenise() the prompt side uses, so `code_rag` or `d10`
    style spellings split/normalise into tokens that can actually match instead of
    silently scoring zero (task:21353636 risk 18b43bd9). A group with no `keywords`
    yields an empty set -- unmatched, not an error.
    """
    from langchain_learning.nodes._text_utils import tokenise
    raw = node.get("keywords") or []
    return tokenise(" ".join(raw) if isinstance(raw, list) else str(raw))


def match_groups(prompt_keywords: set[str], graph: dict | None = None) -> dict[str, list[str]]:
    """Group id -> the prompt keywords that matched its curated keywords (exact token).

    Exact-token, not the DB scorer's substring test: substring on curated words would
    let `get` match `target`. Fail-soft like the rest of this module -- a missing or
    malformed graph yields {}.
    """
    graph = graph if graph is not None else load_graph()
    if not graph or not prompt_keywords:
        return {}
    try:
        out = {}
        for n in graph["nodes"]:
            if n["kind"] != "tool_group":
                continue
            hit = sorted(prompt_keywords & group_keywords(n))
            if hit:
                out[n["id"]] = hit
        return out
    except (KeyError, TypeError, AttributeError) as exc:
        _log.warning("[tool_groups] malformed graph: %r", exc)
        return {}


def route_groups(hints: list[dict], graph: dict | None = None, top_groups: int = 3) -> list[dict]:
    """Rank the groups the hinted tools fall in. `hints` is ScoreToolsNode output
    (already best-first); groups rank by hit count, then by best hint position."""
    graph = graph if graph is not None else load_graph()
    if not graph or not hints:
        return []
    try:
        routes = _route(hints, graph, top_groups)
    except (KeyError, TypeError, AttributeError) as exc:
        # Valid JSON with the wrong shape must degrade to no routing, not break UPS.
        _log.warning("[tool_groups] malformed graph: %r", exc)
        return []
    # Success is logged too: without it a working router and a silent one look the
    # same in hook_logs (task:750242a5).
    if routes:
        _log.info("[tool_groups] routed %d hint(s) to %d group(s): %s", len(hints), len(routes),
                  ", ".join(f"{r['group']}({len(r['matched'])})" for r in routes))
    else:
        _log.info("[tool_groups] no group for %d hint(s)", len(hints))
    return routes


def _route(hints: list[dict], graph: dict, top_groups: int) -> list[dict]:
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
