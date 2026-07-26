"""Live session/task-activation lookups against the in-process MemorySaver checkpoint.

Extracted out of hooks/ui/deps.py (epic:a6216a10, task:050ee644) — these two
functions back the production /session/active and /session/current endpoints
in hooks/server.py, consumed by mcp__claude-hooks__hooks__session_id and other
MCP tools. They are not UI-dashboard code and must survive the /ui removal.
"""
from __future__ import annotations

import time as _time

from src.logger import get_logger as _get_logger

_log = _get_logger(__name__)


def get_active_session() -> dict:
    """Return the active task from the most recent session checkpoint.

    Skips done/abandoned tasks even if the checkpoint is stale.
    """
    try:
        import langchain_learning.session_graph as sg
        checkpointer = getattr(sg._graph, "checkpointer", None)
        if not checkpointer:
            return {}
        from src.tools.tasks import handle_get
        latest = next(iter(checkpointer.list(None)), None)
        if not latest:
            return {}
        state = latest.checkpoint.get("channel_values", {})
        task_id = state.get("active_task_id", "")
        if not task_id:
            return {}
        t = handle_get(task_id)
        if t.get("status") in ("done", "abandoned"):
            return {}
        return {
            "task_id": task_id,
            "title": state.get("active_task_title", ""),
            "session_id": latest.config["configurable"]["thread_id"],
            "turn": state.get("turn", 0),
        }
    except Exception:
        return {}


def get_current_session() -> dict:
    """Return {session_id, turn} from the single most-recent checkpoint write.

    Unlike get_active_session, does NOT require an active task — this is the
    only signal available before any task has been activated in a session.
    Returns {} if the graph has no checkpointer or no checkpoint exists yet
    (e.g. called before the first UserPromptSubmit of a brand-new session has
    finished writing its checkpoint — a real race, not just a hypothetical one).
    """
    try:
        import langchain_learning.session_graph as sg
        checkpointer = getattr(sg._graph, "checkpointer", None)
        if not checkpointer:
            return {}
        _t0 = _time.perf_counter()
        latest = next(iter(checkpointer.list(None)), None)
        _elapsed_ms = (_time.perf_counter() - _t0) * 1000
        # >500ms is a real anomaly here — checkpointer.list(None) reading one
        # row should be near-instant. Logged unconditionally (not just when
        # slow) so a regression is visible in normal logs, not only when
        # someone thinks to check. task:b3964f85 — the checkpointer is now
        # MemorySaver (in-process dict), not a SQLite file, so there's no
        # db_size to report anymore; a slow read here would now point at
        # unbounded *total* checkpoint count across all threads instead of
        # file bloat (per-thread history is capped by NoopNode, but nothing
        # yet caps the number of distinct threads kept in RAM — see
        # task:b3964f85's follow-up note on cross-thread growth).
        if _elapsed_ms > 500:
            _log.warning("[get_current_session] checkpointer.list(None) took %.0fms — possible unbounded checkpoint growth", _elapsed_ms)
        else:
            _log.debug("[get_current_session] checkpointer.list(None) took %.1fms", _elapsed_ms)
        if not latest:
            return {}
        state = latest.checkpoint.get("channel_values", {})
        return {
            "session_id": latest.config["configurable"]["thread_id"],
            "turn": state.get("turn", 0),
        }
    except Exception as exc:
        _log.warning("[get_current_session] failed: %s", exc)
        return {}
