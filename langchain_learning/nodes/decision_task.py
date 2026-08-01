"""DecisionTaskNode — PostToolUse node for tasks__add_decision.

Appends the decision text to mid_task_decisions in the checkpoint so it
is injected on every subsequent UPS turn. This is the PostToolUse bridge
that keeps checkpoint writes out of the MCP layer.

Tags: task-decision, post-tool-use, checkpoint, mid-task-decisions
"""
from __future__ import annotations

from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)

# mid_task_decisions exists solely to be injected into the prompt each turn (see
# module docstring) — not a durable record, so capping to the most recent N here
# is safe; the full decision history still lives in the task's DB record.
_MAX_DECISIONS = 15


class DecisionTaskNode:
    """PostToolUse bridge for tasks__add_decision.

    Reads decision text from tool_input, appends it to mid_task_decisions
    in state so the checkpoint carries it forward. No-ops for other tools.

    Tags: task-decision, post-tool-use, checkpoint, mid-task-decisions
    """

    def __call__(self, state: SessionState) -> dict:
        entry("decision_task", state)

        tool_name = state.get("tool_name", "")
        if tool_name != "tasks__add_decision":
            return {}

        tool_input = state.get("tool_input") or {}
        decision   = str(tool_input.get("decision", "")).strip()
        task_id    = str(tool_input.get("task_id", ""))
        session_id = str(state.get("session_id", ""))[:8]

        if not decision:
            _log.warning("[decision_task] session=%s task=%s — empty decision text, skipping", session_id, task_id)
            return {}

        current  = list(state.get("mid_task_decisions") or [])
        current.append(decision)
        if len(current) > _MAX_DECISIONS:
            current = current[-_MAX_DECISIONS:]
        _log.info(
            "[decision_task] session=%s task=%s decisions=%d text=%r",
            session_id, task_id, len(current), decision[:80],
        )
        return {"mid_task_decisions": current}
