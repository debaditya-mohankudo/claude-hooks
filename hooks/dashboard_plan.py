"""Mirror Claude's plan into a JSON file the kitty dashboard renders.

Called from client.py on PostToolUse, next to kitty_status. Sources:
- TodoWrite            -> replaces the plan with the todo list
- tasks__create        -> plan = the task title + its resolution checklist
- tasks__check_item    -> ticks one item (only if the plan is that task's)

Hook-owned file (~/.claude/dashboard_plan.json, CLAUDE_DASHBOARD_PLAN
overrides); the free-text dashboard_note.md stays Claude's to write. Never
raises, never blocks; with CLAUDE_DASHBOARD_PLAN=0 it does nothing.
"""
import json
import os
import tempfile
from pathlib import Path

_DEFAULT = "~/.claude/dashboard_plan.json"


def _path() -> Path | None:
    p = os.environ.get("CLAUDE_DASHBOARD_PLAN", _DEFAULT)
    return None if p == "0" else Path(p).expanduser()


def _short(name: str) -> str:
    return name.rsplit("__", 2)[-2:][-1] if name.startswith("mcp__") else name


def new_plan(payload: dict, current: dict | None) -> dict | None:
    """Plan after this PostToolUse payload, or None to leave it as is."""
    tool = _short(payload.get("tool_name", ""))
    args = payload.get("tool_input") or {}
    resp = payload.get("tool_response")
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except ValueError:
            resp = None
    if tool == "TodoWrite":
        items = [{"text": t.get("content", ""), "done": t.get("status") == "completed"}
                 for t in args.get("todos", [])]
        return {"source": "todo", "title": "", "items": items}
    if tool == "create" and isinstance(resp, dict) and resp.get("ok"):
        return {"source": "task", "task_id": resp.get("id"), "title": args.get("title", ""),
                "items": [{"text": t, "done": False} for t in args.get("resolution") or []]}
    if tool == "check_item" and current and current.get("task_id") == args.get("task_id"):
        i = args.get("index")
        items = current.get("items", [])
        if isinstance(i, int) and 0 <= i < len(items):
            items[i]["done"] = bool(args.get("done", True))
            return current
    return None


def update(event: str, payload: dict | None = None) -> None:
    try:
        path = _path()
        if path is None or event != "PostToolUse" or not payload:
            return
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError):
            current = None
        plan = new_plan(payload, current)
        if plan is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as f:
            json.dump(plan, f)
        os.replace(f.name, path)
    except Exception:
        pass
