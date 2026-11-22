"""Writer-side pin of the state files `mac kitty dashboard` (cli-graph) reads.

The viewer moved to cli-graph in task:3f30d74d; these files are now the only
interface between the repos. cli-graph's tests/test_kitty_dashboard.py pins the
same shapes from the reader side. No import of cli-graph here (fail-open: the
hooks must not depend on it), so a writer-side change fails THIS test, and the
fix is to update both pins together.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import dashboard_plan as dp  # noqa: E402
import dashboard_turn as dt  # noqa: E402


def _post(tool, args, resp=None):
    return {"tool_name": tool, "tool_input": args, "tool_response": resp}


def test_plan_shape_todo(monkeypatch, tmp_path):
    f = tmp_path / "plan.json"
    monkeypatch.setenv("CLAUDE_DASHBOARD_PLAN", str(f))
    dp.update("PostToolUse", _post("TodoWrite", {"todos": [{"content": "a", "status": "pending"}]}))
    assert json.loads(f.read_text()) == {
        "source": "todo", "title": "", "items": [{"text": "a", "done": False}]}


def test_plan_shape_task(monkeypatch, tmp_path):
    f = tmp_path / "plan.json"
    monkeypatch.setenv("CLAUDE_DASHBOARD_PLAN", str(f))
    dp.update("PostToolUse", _post(
        "mcp__taskfw__tasks__create", {"title": "T", "resolution": ["x"]}, {"ok": True, "id": "abc"}))
    assert json.loads(f.read_text()) == {
        "source": "task", "task_id": "abc", "title": "T", "items": [{"text": "x", "done": False}]}


def test_turn_shape(monkeypatch, tmp_path):
    f = tmp_path / "turn.json"
    monkeypatch.setenv("CLAUDE_DASHBOARD_TURN", str(f))
    dt.update("UserPromptSubmit", {"prompt": "do x"})
    assert json.loads(f.read_text()) == {"prompt": "do x", "summary": ""}
