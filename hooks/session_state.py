"""Live session lookup against hooks/session_store.py.

Backs the production /session/current endpoint in hooks/server.py, consumed by
`mac hooks session_id` and other callers. (Extracted from hooks/ui/deps.py in
epic:a6216a10, task:050ee644.)

Removed: get_active_session() (task:8529435a) and set_active_task/get_active_task
(task:996cc8f0, gone in task:173e6846): task-framework owns the active task, so
ask tasks__active. This module only ever reports session_id/turn, never a task id.
The LangGraph MemorySaver this used to read, with its cross-thread
_latest_checkpoint_tuple workaround, went with task:31fb711d.
"""
from __future__ import annotations

from src.logger import get_logger as _get_logger

_log = _get_logger(__name__)


def get_current_session() -> dict:
    """Return {session_id, turn} of the most recently touched real session, or {} if none yet.

    Needs no active task: it is the only signal available before a task is activated.
    """
    try:
        from hooks import session_store
        return session_store.current()
    except Exception as exc:
        _log.warning("[get_current_session] failed: %s", exc)
        return {}
