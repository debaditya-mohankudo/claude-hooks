"""LogTaskEventsNode — appends a turn event to the active task at UPS time.

Runs at the end of the UserPromptSubmit chain (after set_prompt_id). Reads the
prompt directly from state["prompt"] — no tmp file needed. Tools column is empty
at insert time; PostToolUse upserts tool names into it as tools fire.

Auto-closes the task when the prompt contains "task:<id> done" — the only
recognized completion signal. Explicit convention prevents false positives from
normal progress updates ("all tests passing", "it now works", etc.).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from langchain_learning.config import config as _cfg
from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)

_MAX_SUMMARY = 200

# Only recognized completion signal — explicit and unambiguous
_TASK_DONE_PATTERN = re.compile(r"\btask:[a-f0-9]{6,}\s+done\b", re.IGNORECASE)

# One-shot nudge surfaced through the UPS hook response after an auto-close.
# run_session() clears pending_hook_output from the checkpoint after the
# dispatcher reads it — without that clear it would leak into the next
# PostToolUse response (run_post_tool returns whatever is in the field).
# Shared with DeactivateTaskNode's tasks__finish pointer (task:8c3c2ee4) so
# the two close paths can't drift.
INTROSPECTION_NUDGE_TEMPLATE = (
    "task:{task_id} closed — run /task-introspection task:{task_id} "
    "to capture decisions and learnings while the context is fresh."
)


def _is_completion_signal(text: str) -> bool:
    return bool(_TASK_DONE_PATTERN.search(text))



class LogTaskEventsNode:
    """Write one task_event row per UPS turn for the active task.

    Runs at the end of the UserPromptSubmit chain (after set_prompt_id).
    Reads prompt directly from state["prompt"] — no tmp file needed.
    Tools column is inserted as empty string; PostToolUse upserts tool names
    as they fire via the prompt_id FK.

    Auto-completion detection: closes the task when the prompt contains "task:<id> done".
    No secondary heuristics — explicit signal only to prevent false positives.

    Tags: task-events, task-history, user-prompt-submit, auto-completion, task-logging
    """

    def __call__(self, state: SessionState) -> dict:
        entry("log_task_events", state)

        task_id = state.get("active_task_id", "")
        if not task_id:
            _log.info("[log_task_events] no active task — skipping")
            return {}

        # Lazy import — session_graph imports the node registry, which imports
        # this module, so a top-level import here would be circular.
        from langchain_learning.session_graph import TEST_SESSION_PREFIX
        session_id_check = state.get("session_id", "")
        if session_id_check.startswith(TEST_SESSION_PREFIX):
            # replay_harness.py (task:a10a6638) deliberately seeds active_task_id
            # to exercise task-aware nodes during replay, but that must not write
            # real rows into proj_tasks.db's task_events table — a persistent,
            # cross-process store, unlike the in-memory checkpoint this prefix
            # already isolates (task:b63088a1). Skip the DB write, not the node.
            _log.debug("[log_task_events] session=%s is test-prefixed — skipping task_events write", session_id_check[:8])
            return {}

        if not _cfg.tasks_db.exists():
            _log.warning("[log_task_events] proj_tasks.db not found")
            return {}

        prompt_id  = state.get("prompt_id", "")
        session_id = state.get("session_id", "")
        turn       = state.get("turn", 0)
        full_text  = (state.get("prompt") or "").strip()
        summary    = full_text[:_MAX_SUMMARY]
        related    = ",".join(t["id"] for t in (state.get("related_tasks") or []) if t.get("id"))
        memories   = ",".join(m["name"] for m in (state.get("memories") or []) if m.get("name"))

        auto_completed = _is_completion_signal(full_text)
        if not auto_completed and "done" in full_text.lower() and "task:" in full_text.lower():
            _log.debug(
                "[log_task_events] 'done' + 'task:' in prompt but pattern did not match "
                "(task IDs must be pure hex [a-f0-9]{6,}) — prompt excerpt: %.80s",
                full_text,
            )

        try:
            with sqlite3.connect(str(_cfg.tasks_db), timeout=5) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO task_events
                       (task_id, prompt_id, session_id, turn, summary, tools, related, memories)
                       VALUES (?, ?, ?, ?, ?, '', ?, ?)""",
                    (task_id, prompt_id, session_id, turn, summary, related, memories),
                )
                conn.execute(
                    "UPDATE open_tasks SET updated_at=datetime('now') WHERE id=?",
                    (task_id,),
                )
            if auto_completed:
                from src.tools.tasks import handle_update
                result = handle_update(id=task_id, status="done")
                if "error" in result:
                    _log.warning("[log_task_events] auto-done transition blocked task=%s: %s", task_id, result["error"])
                    auto_completed = False  # don't clear checkpoint if transition failed
                else:
                    _log.info("[log_task_events] auto-moved to done task=%s", task_id)
            _log.info("[log_task_events] logged task=%s turn=%d prompt_id=%s memories=%s auto_completed=%s",
                      task_id, turn, prompt_id[:8] if prompt_id else "?", memories or "(none)", auto_completed)
        except Exception as exc:
            _log.error("[log_task_events] DB error: %s", exc)
            return {}

        if auto_completed and session_id:
            return {
                "active_task_id": "", "active_task_title": "",
                "active_parent_task_id": "", "active_parent_task_title": "",
                "task_body": "", "task_memories": [], "task_context": [],
                "task_rag_chunks": [], "task_stack": [], "mid_task_decisions": [],
                "related_tasks": [], "related_commits": [],
                "execution_contract": "",
                "pending_hook_output": {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": INTROSPECTION_NUDGE_TEMPLATE.format(task_id=task_id),
                    }
                },
            }

        return {}
