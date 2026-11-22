"""hooks/dashboard_plan.py: PostToolUse payloads -> plan JSON the dashboard renders."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "hooks"))
import dashboard_plan as dp  # noqa: E402

# The renderer cases (read_plan) moved to cli-graph with the viewer: task:3f30d74d.


def _post(tool, args, resp=None):
    return {"tool_name": tool, "tool_input": args, "tool_response": resp}


def _run(monkeypatch, tmp_path, *payloads):
    f = tmp_path / "plan.json"
    monkeypatch.setenv("CLAUDE_DASHBOARD_PLAN", str(f))
    for p in payloads:
        dp.update("PostToolUse", p)
    return f


def test_todowrite_replaces_plan(monkeypatch, tmp_path):
    f = _run(monkeypatch, tmp_path, _post("TodoWrite", {"todos": [
        {"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}]}))
    assert json.loads(f.read_text())["items"] == [{"text": "a", "done": True}, {"text": "b", "done": False}]


def test_taskfw_create_then_check_item(monkeypatch, tmp_path):
    f = _run(monkeypatch, tmp_path,
             _post("mcp__taskfw__tasks__create", {"title": "T", "resolution": ["x", "y"]}, {"ok": True, "id": "abc"}),
             _post("mcp__taskfw__tasks__check_item", {"task_id": "abc", "index": 1}, {"ok": True}),
             _post("mcp__taskfw__tasks__check_item", {"task_id": "other", "index": 0}, {"ok": True}))
    plan = json.loads(f.read_text())
    assert plan["title"] == "T" and [i["done"] for i in plan["items"]] == [False, True]


def test_other_events_tools_and_off_switch_leave_plan_alone(monkeypatch, tmp_path):
    f = _run(monkeypatch, tmp_path, _post("Bash", {"command": "ls"}))
    dp.update("PreToolUse", _post("TodoWrite", {"todos": []}))
    assert not f.exists()
    monkeypatch.setenv("CLAUDE_DASHBOARD_PLAN", "0")
    dp.update("PostToolUse", _post("TodoWrite", {"todos": [{"content": "a"}]}))
    assert not f.exists()
