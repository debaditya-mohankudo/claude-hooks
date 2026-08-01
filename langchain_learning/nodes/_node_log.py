"""Shared node entry logging helper.

Log convention:
  [node]  phase=parallel|sequential  event=X  session=X  turn=X  key=val
"""
from __future__ import annotations

from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)

# Nodes that run in the parallel fan-out tier — update when session_graph changes
_PARALLEL_NODES = frozenset({
    "cwd_domain_detect", "load_memories", "score_tools",
    "load_task_code", "load_related_commits",
})


def entry(node: str, state: SessionState, **extra) -> None:
    """Log node entry with phase, event_type, session, turn, and any extras."""
    phase = "parallel" if node in _PARALLEL_NODES else "sequential"
    _log.info(
        "[%s] phase=%s event=%s session=%s turn=%s %s",
        node,
        phase,
        state.get("event_type", "?"),
        (state.get("session_id") or "")[:8] or "?",
        state.get("turn", "?"),
        " ".join(f"{k}={v}" for k, v in extra.items()),
    )
