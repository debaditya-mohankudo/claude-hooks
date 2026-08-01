"""LoadActiveTaskNode — reads active_task_id from checkpoint."""
from __future__ import annotations

from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)


class LoadActiveTaskNode:
    """Pass-through node — active_task_id already lives in checkpoint state.

    Used to filter the task out for this turn when its project:<name> tag
    disagreed with the current cwd. That suppression, and the _project_from_cwd
    helper and _cfg import it needed, were removed here (task:87ec7876): the
    tag was derived from a working directory (a guess recorded as a fact) and
    read a store this repo no longer owns. task-framework scopes the active
    task by workspace path directly, so what this approximated is now answered
    exactly, by the system that owns it. This node now only logs.

    Tags: task-activation, active-task, checkpoint
    """

    def __call__(self, state: SessionState) -> dict:
        entry("load_active_task", state)
        task_id = state.get("active_task_id", "")
        if not task_id:
            return {}

        # The project-scoping suppression is gone (task:6240c675). It read the
        # task's project:<name> tag out of proj_tasks.db and blanked the active
        # task when the cwd disagreed. Two reasons it goes: the tag was derived
        # from a working directory, which is a guess recorded as a fact, and the
        # lookup read a store this repo no longer owns. task-framework scopes the
        # active task by workspace path directly, so the thing this approximated
        # is now answered exactly, by the system that owns it.
        _log.info("[load_active_task] session=%s active_task=%s title=%r",
                  (state.get("session_id") or "")[:8], task_id,
                  state.get("active_task_title", ""))
        return {}
