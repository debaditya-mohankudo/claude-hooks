"""User-context ontology -> ambient system-prompt blocks.

Source of truth: <VAULT_ROOT>/LIFE_OS/user-context-ontology.json, a typed
graph (meta / relation_types / nodes / edges) of what the user does via
Claude -- persona, financial profile, and one node per work domain. Replaces
the single dev_personality.md ambient file, which is still read verbatim as a
fallback when the JSON is absent or unparseable so persona context is never
dropped.

Two render tiers (see LIFE_OS/user-context-ontology.md for the spec):
  - always_on nodes  -> rendered every turn, grouped by node type
  - every other node -> a `## Relevant context` line only when >= _MIN_TAG_HITS
    of its `tags` tokens appear in the prompt

iCloud's file provider locks vault files during sync (OSError EDEADLK) and the
JSON may be stored dataless, so a failed read falls back to the last good copy
in the persistent "user_context" cache (hooks/cache_store.py), same pattern
the old _load_vault_context used.
"""
from __future__ import annotations

import json

from hooks.cache_store import get_cache
from hooks.paths import VAULT_ROOT
from langchain_learning.nodes._text_utils import tokenise
from src.logger import get_logger

log = get_logger(__name__)

_ONTOLOGY_PATH = VAULT_ROOT / "LIFE_OS" / "user-context-ontology.json"
_FALLBACK_MD   = VAULT_ROOT / "LIFE_OS" / "dev_personality.md"

# A non-always_on node joins `## Relevant context` when at least this many of
# its curated tag tokens appear in the prompt. Tags are hand-authored keywords
# meant exactly for this match, so a small raw count beats a Jaccard ratio
# (which the long tag lists would push out of reach).
_MIN_TAG_HITS = 2

# node.type -> heading for always_on nodes; types not listed fall back to the
# node's own label.
_SECTION_HEADINGS = {
    "persona":   "Dev personality",
    "financial": "Financial profile",
}

_cache = get_cache("user_context", persist=True)


def _load_graph() -> dict | None:
    """Parsed ontology graph, or None. EDEADLK-safe via the persistent cache."""
    try:
        text = _ONTOLOGY_PATH.read_text(encoding="utf-8")
        graph = json.loads(text)
        _cache["json"] = text
        return graph
    except FileNotFoundError:
        return None
    except Exception as exc:
        cached = _cache.get("json")
        if cached:
            log.info("user_context: read/parse failed (%s), using cached copy", exc)
            try:
                return json.loads(cached)
            except Exception:
                return None
        log.warning("user_context: failed to read %s: %s", _ONTOLOGY_PATH, exc)
        return None


def _node_text(node: dict) -> str:
    return (node.get("render") or node.get("definition") or "").strip()


def _render_always_on(graph: dict) -> str:
    """One heading + body block per always_on node, _SECTION_HEADINGS types first."""
    nodes = [n for n in graph.get("nodes", []) if n.get("always_on")]
    if not nodes:
        return ""
    ordered_types = list(_SECTION_HEADINGS) + sorted(
        {n.get("type", "") for n in nodes} - set(_SECTION_HEADINGS)
    )
    lines: list[str] = []
    for typ in ordered_types:
        for node in (n for n in nodes if n.get("type") == typ):
            body = _node_text(node)
            if not body:
                continue
            heading = _SECTION_HEADINGS.get(typ) or node.get("label", typ).strip()
            lines += [f"## {heading}", body, ""]
    return "\n".join(lines).strip()


def _score_nodes(graph: dict, prompt_tokens: set[str]) -> str:
    """`## Relevant context` block for non-always_on nodes whose tags hit the prompt."""
    if not prompt_tokens:
        return ""
    hits: list[tuple[int, dict]] = []
    for node in graph.get("nodes", []):
        if node.get("always_on"):
            continue
        tag_tokens = tokenise(node.get("tags", ""))
        n = len(prompt_tokens & tag_tokens)
        if n >= _MIN_TAG_HITS:
            hits.append((n, node))
    if not hits:
        return ""
    hits.sort(key=lambda h: h[0], reverse=True)
    lines = ["## Relevant context"]
    for _, node in hits:
        label = node.get("label") or node.get("id", "?")
        lines.append(f"- **{label}**: {node.get('definition', '').strip()}")
    return "\n".join(lines).strip()


def _fallback_dev_personality() -> str:
    """dev_personality.md verbatim, its H1 demoted to H2 to match _format_system_prompt."""
    try:
        text = _FALLBACK_MD.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    return text.replace("# Dev personality", "## Dev personality", 1) if text else ""


def render_user_context(prompt_tokens: set[str] | None = None) -> str:
    """Ambient block for the turn: always_on render + scored `## Relevant context`.

    Falls back to dev_personality.md when the ontology JSON is unavailable.
    """
    graph = _load_graph()
    if graph is None:
        return _fallback_dev_personality()
    parts = [_render_always_on(graph), _score_nodes(graph, prompt_tokens or set())]
    return "\n\n".join(p for p in parts if p).strip()
