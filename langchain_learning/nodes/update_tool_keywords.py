"""UpdateToolKeywordsNode — derive and persist tool-name keywords on first insert."""
from __future__ import annotations

import re
import sqlite3

from langchain_learning.config import config as _cfg
from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)

_SPLIT_RE = re.compile(r"[_\-]+")


def _tool_keywords(tool_name: str, domain: str, skill: str) -> str:
    """Derive keywords from tool name tokens + domain + skill.

    Strips MCP prefix (mcp__<server>__) and splits on underscores/hyphens.
    Never uses prompt text — these are structural, tool-specific tokens.
    """
    # strip mcp__<server>__ prefix
    name = re.sub(r"^mcp__[^_]+__", "", tool_name)
    tokens = {t for t in _SPLIT_RE.split(name) if len(t) >= 3}
    if domain:
        tokens.add(domain)
    if skill:
        tokens.update(t for t in _SPLIT_RE.split(skill) if len(t) >= 3)
    return ",".join(sorted(tokens))


class UpdateToolKeywordsNode:
    """Write tool-derived keywords to mcp_tool_hints — only on first insert, never overwritten.

    Tags: tool-hints, tool-keywords, post-tool-use, tool-registry, first-insert
    """

    def __call__(self, state: SessionState) -> dict:
        from core.tool_registry import infer_domain, infer_skill

        tool_name = state.get("tool_name", "")
        entry("update_tool_keywords", state, tool=tool_name)

        if not tool_name or not _cfg.tool_hints_db.exists():
            return {}

        domain = infer_domain(tool_name)
        skill  = infer_skill(tool_name)
        kw     = _tool_keywords(tool_name, domain, skill)

        try:
            with sqlite3.connect(str(_cfg.tool_hints_db)) as conn:
                existing = conn.execute(
                    "SELECT keywords FROM mcp_tool_hints WHERE tool_name = ?", (tool_name,)
                ).fetchone()
                if existing is not None and not existing[0]:
                    conn.execute(
                        "UPDATE mcp_tool_hints SET keywords = ? WHERE tool_name = ?",
                        (kw, tool_name),
                    )
                    _log.info("[update_tool_keywords] seeded tool=%s kw=%s", tool_name, kw)
        except Exception as exc:
            _log.warning("[update_tool_keywords] failed for %r: %s", tool_name, exc)

        return {}
