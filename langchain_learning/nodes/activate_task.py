"""ActivateTaskNode — PostToolUse node for task activation and stack pop.

Handles:
  tasks__set_active  — reads task_id from tool_input, activates task in checkpoint
  tasks__pop_active  — pops the task_stack and re-activates the previous task

Emits task_files + active_task_domain into state for the downstream backfill
slot (BackfillNodeProtocol). Does not perform backfill itself.

DB logic is inlined (not delegated to SetActiveTaskNode/LoadTaskMemoriesNode) so
those nodes' entry() calls don't pollute the PostToolUse log stream with wrong event context.

Tags: task-activation, post-tool-use, checkpoint, active-task, task-stack
"""
from __future__ import annotations

import re
import sqlite3

from pathlib import Path

from langchain_learning.config import config as _cfg
from langchain_learning.nodes._memory_scoring import score_memories
from langchain_learning.nodes._node_log import entry
from langchain_learning.nodes._text_utils import tokenise, task_project_tag
from langchain_learning.nodes.backfill_memory_files import _parse_files_section, _file_tokens
from langchain_learning.session_state import SessionState
from repo_memory.store import RepoMemoryStore
from repo_memory.store import resolve_path as _resolve_repo_memory_path
from src.logger import get_logger

_log = get_logger(__name__)

_ACTIVATING_TOOLS = {"tasks__set_active", "tasks__pop_active"}

# Fixed north-star block — byte-identical every turn while a task is active, unlike
# the dynamic sections (memories, related tasks/commits, task history) which are
# re-derived and budget/truncation-trimmed each turn. Only {task_id}/{title} vary.
#
# This is the compressed, pinned counterpart to skills/task-implementation/skill.md
# (the full execution loop, warning signs, and engineering principles) — it exists
# because checkpoint injection survives context compaction on long tasks, which a
# skill invoked once cannot guarantee. Keep this short; expand the philosophy in the
# skill, not here, to avoid two independently-maintained copies drifting apart.
_EXECUTION_CONTRACT_TEMPLATE = """You are executing task:{task_id} — {title}.

Every action should move this task toward completion. Do not optimize for
exploration; optimize for finishing the current objective.

Before using a tool, ask yourself:
- Does this reduce uncertainty?
- Does this directly advance implementation?
- Am I repeating work?
- Is there a smaller next step?

1. Keep the task objective in focus.
2. Prefer the smallest action that increases confidence or delivers progress.
3. Search only until you can act.
4. Validate assumptions before building on them.
5. Replan when evidence changes.
6. Detect repeated work and change strategy.
7. Capture durable knowledge when discovered.
8. Finish decisively rather than optimizing endlessly.

See /task-implementation for the full execution loop and warning signs."""


def _build_execution_contract(task_id: str, title: str) -> str:
    return _EXECUTION_CONTRACT_TEMPLATE.format(task_id=task_id, title=title)


def _lookup_task(task_id: str) -> tuple[str, str, str, str] | None:
    """Return (title, body, parent_id, parent_title) for task_id. None if not found.

    Does NOT write status to the DB — active-task tracking is checkpoint-only
    (memory: tasks-active-status-checkpoint-only). A prior version of this
    function attempted handle_update(status="active") here, but "active" has
    never been a valid DB status (_VALID_STATUSES in src/tools/tasks.py), so
    that call failed on every single activation, logging a WARNING every time
    for no effect — dead code, removed (found via log audit 2026-07-26).
    """
    if not _cfg.tasks_db.exists():
        return None
    try:
        with sqlite3.connect(str(_cfg.tasks_db), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT title, body, status, parent_id FROM open_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            parent_id, parent_title = lookup_parent_task(conn, row)
    except Exception as exc:
        _log.error("[activate_task] DB error looking up task=%s: %s", task_id, exc)
        return None

    return row["title"], row["body"] or "", parent_id, parent_title

def lookup_parent_task(conn, row):
    parent_id = row["parent_id"] or ""
    parent_title = ""
    if parent_id:
        p = conn.execute(
                    "SELECT title FROM open_tasks WHERE id = ?", (parent_id,)
                ).fetchone()
        if p:
            parent_title = p["title"]
    return parent_id,parent_title


def _score_memories(task_id: str, task_title: str, task_body: str = "") -> list[dict]:
    """Score MEMORY.sqlite rows against task title + body using combination signals."""
    tokens = set(tokenise(f"{task_title} {task_body}".lower()))
    if not tokens or not _cfg.memory_db.exists():
        return []
    project_domain = task_project_tag(task_id, _cfg.tasks_db)
    try:
        conn = sqlite3.connect(f"file:{_cfg.memory_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        memories = score_memories(tokens, project_domain, conn)
        conn.close()
    except Exception as exc:
        _log.warning("[activate_task] memory DB error: %s", exc)
        return []
    return memories



def _repo_memory_store_path(cwd: str) -> Path | None:
    """Path to cwd's repo_memory/memories.json if migrated, else None.

    A migrated repo's presence of this file is the switch: task-activation
    memory context comes from repo_memory instead of MEMORY.sqlite's global
    scoring for that repo (task:850ddd65 — repo memories replace task
    memories, not add to them, once a repo has moved to the split store).

    Thin wrapper over repo_memory.store.resolve_path — same resolution used
    by LoadMemoriesNode's per-turn scoring (_memory_scoring.score_repo_memories),
    kept as a separate function here only for call-site readability.
    """
    return _resolve_repo_memory_path(cwd)


def _load_repo_task_memories(store_path: Path, task_files: list[str]) -> list[dict]:
    """Load repo_memory/memories.json entries, filtered by overlap with the
    task's Files: section when a Files: section exists; otherwise all of them
    (this store is small/curated, unlike the global scored table, so showing
    everything when there's nothing to filter by is intentional, not noise).
    """
    try:
        store = RepoMemoryStore(store_path)
    except Exception as exc:
        _log.warning("[activate_task] repo_memory load error: %s", exc)
        return []

    all_memories = store.list()
    if not task_files:
        return all_memories

    # Same stem-token matching backfill_memory_files.py already uses for the
    # global store's files-column overlap — reuse it rather than reinventing
    # a second matching heuristic for this store.
    task_tokens = _file_tokens(task_files)
    matched = []
    for memory in all_memories:
        mem_files = [f.strip() for f in (memory.get("files") or "").split(",") if f.strip()]
        if task_tokens & _file_tokens(mem_files):
            matched.append(memory)
    return matched


def _activate(state: SessionState, task_id: str, task_stack: list) -> dict:
    """Resolve task from DB + score memories. Returns state update dict.

    Emits task_files + active_task_domain for the downstream backfill slot.
    """
    result = _lookup_task(task_id)
    if result is None:
        _log.warning("[activate_task] task_id=%s not found in proj_tasks.db", task_id)
        return {}
    title, body, parent_id, parent_title = result
    domain = task_project_tag(task_id, _cfg.tasks_db) or "global"
    task_files = _parse_files_section(body)

    repo_store_path = _repo_memory_store_path(state.get("cwd", ""))
    if repo_store_path is not None:
        # Migrated repo: repo_memory is the source of truth for project/reference
        # facts about this repo now — skip the global scorer entirely rather than
        # showing both (the global table no longer holds this repo's facts once
        # migrated, so calling it would just waste a query, but the skip is
        # explicit here rather than incidental).
        memories = []
        repo_task_memories = _load_repo_task_memories(repo_store_path, task_files)
    else:
        memories = _score_memories(task_id, title, body)
        repo_task_memories = []

    return {
        "active_task_id":           task_id,
        "active_task_title":        title,
        "task_body":                body,
        "task_memories":            memories,
        "repo_task_memories":       repo_task_memories,
        "task_stack":               task_stack,
        "active_parent_task_id":    parent_id,
        "active_parent_task_title": parent_title,
        "active_task_domain":       domain,
        "task_files":               task_files,
        "execution_contract":       _build_execution_contract(task_id, title),
    }


class ActivateTaskNode:
    """PostToolUse bridge for tasks__set_active and tasks__pop_active.

    tasks__set_active: reads task_id from tool_input, activates task in checkpoint.
    tasks__pop_active: pops task_stack and re-activates the previous task.
    No-ops for any other tool name.

    Tags: task-activation, post-tool-use, checkpoint, active-task, task-stack
    """

    def __call__(self, state: SessionState) -> dict:
        entry("activate_task", state)

        tool_name  = state.get("tool_name", "")
        session_id = str(state.get("session_id", ""))

        if tool_name not in _ACTIVATING_TOOLS:
            _log.debug("[activate_task] tool=%s not an activating tool — skip", tool_name)
            return {}

        if tool_name == "tasks__set_active":
            task_id = (state.get("tool_input") or {}).get("task_id", "")
            if not task_id:
                _log.warning("[activate_task] tasks__set_active fired but tool_input has no task_id")
                return {}
            current_active = state.get("active_task_id", "")
            stack = list(state.get("task_stack") or [])
            if current_active and current_active != task_id:
                stack.append(current_active)
                _log.info("[activate_task] pushed %s onto stack (depth=%d)", current_active, len(stack))
            updates = _activate(state, task_id, stack)

        else:  # tasks__pop_active
            stack = list(state.get("task_stack") or [])
            if not stack:
                _log.info("[activate_task] pop on empty stack — clearing active task for session=%s", session_id[:8])
                return {
                    "active_task_id": "", "active_task_title": "", "task_body": "",
                    "task_memories": [], "repo_task_memories": [], "task_stack": [],
                    "mid_task_decisions": [], "execution_contract": "",
                }
            task_id = stack.pop()
            _log.info("[activate_task] popped task=%s from stack (remaining=%d)", task_id, len(stack))
            updates = _activate(state, task_id, stack)

        if not updates:
            return {}

        _log.info(
            "[activate_task] session=%s tool=%s task=%s title=%r memories=%d repo_memories=%d stack_depth=%d",
            session_id[:8], tool_name, updates.get("active_task_id", ""),
            updates.get("active_task_title", ""),
            len(updates.get("task_memories") or []),
            len(updates.get("repo_task_memories") or []),
            len(updates.get("task_stack") or []),
        )
        return updates
