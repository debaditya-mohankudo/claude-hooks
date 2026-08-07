"""Tests for the SQLite-backed ServerMemory store and its accessor."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "hooks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hooks.server_memory as sm


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """Point ServerMemory at a fresh temp DB + empty session so the real store is never touched."""
    monkeypatch.setattr(sm.ServerMemory, "_DB", tmp_path / "server_memory.sqlite")
    monkeypatch.setattr(sm.ServerMemory, "_cache", [])
    yield


# ── record helpers ────────────────────────────────────────────────────────────

def test_record_prompt_appears_in_events():
    sm.record_prompt("s1", "hello")
    events = sm.get_server_memory()["events"]
    assert events[-1]["type"] == "prompt"
    assert events[-1]["content"] == "hello"
    assert events[-1]["claude_session_id"] == "s1"


def test_empty_prompt_is_noop():
    sm.record_prompt("s1", "")
    assert sm.get_server_memory()["events"] == []


def test_summarize_prompt_is_filtered():
    sm.record_prompt("s1", "Summarize the following task context:\n## Task history")
    assert sm.get_server_memory()["events"] == []


def test_test_session_is_not_recorded():
    sm.record_prompt("pytest-abc", "should be skipped")
    sm.record_tool("test-xyz", "vault__write")
    assert sm.get_server_memory()["events"] == []


def test_record_tool_appears_in_events():
    sm.record_tool("s1", "vault__write")
    events = sm.get_server_memory()["events"]
    assert events[-1]["type"] == "tool"
    assert events[-1]["content"] == "vault__write"


def test_record_task_appears_in_events():
    sm.record_task("s1", "t1", "The Task")
    events = sm.get_server_memory()["events"]
    assert events[-1]["type"] == "task"
    assert events[-1]["content"] == "The Task"
    assert events[-1]["ref"] == "t1"


def test_empty_task_id_is_noop():
    sm.record_task("s1", "", "no id")
    assert sm.get_server_memory()["events"] == []


# ── hook helpers ──────────────────────────────────────────────────────────────

def test_record_tool_from_hook_strips_prefix():
    sm.record_tool_from_hook({"session_id": "s1", "tool_name": "mcp__local-mac__imessage__send"})
    assert sm.get_server_memory()["events"][-1]["content"] == "imessage__send"


def test_record_tool_from_hook_skips_bare_bash():
    # Bash calls are skipped (too noisy) — only git-commit Bash calls are recorded
    before = len(sm.get_server_memory()["events"])
    sm.record_tool_from_hook({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert len(sm.get_server_memory()["events"]) == before


def test_record_tool_from_hook_records_edit():
    sm.record_tool_from_hook({"session_id": "s1", "tool_name": "Edit"})
    assert sm.get_server_memory()["events"][-1]["content"] == "Edit"


# ── unified event sequence ────────────────────────────────────────────────────

def test_events_are_a_chronological_sequence():
    sm.record_prompt("s1", "do a thing")
    sm.record_tool("s1", "vault__read")
    sm.record_task("s1", "t1", "The Task")
    sm.record_tool("s1", "vault__write")
    events = sm.get_server_memory()["events"]
    assert [(e["type"], e["content"]) for e in events] == [
        ("prompt", "do a thing"),
        ("tool", "vault__read"),
        ("task", "The Task"),
        ("tool", "vault__write"),
    ]
    assert events[2]["ref"] == "t1"
    assert all(isinstance(e["ts"], float) for e in events)


def test_get_last_n_events():
    for i in range(10):
        sm.record_prompt("s", f"p{i}")
    out = sm.get_server_memory(n_events=3)
    assert [e["content"] for e in out["events"]] == ["p7", "p8", "p9"]


def test_get_empty_returns_valid_dict():
    out = sm.get_server_memory()
    assert out["events"] == []
    assert out["server_session_id"]


# ── capped at _MAX_ENTRIES ────────────────────────────────────────────────────

def test_capped_to_max_entries(monkeypatch):
    monkeypatch.setattr(sm.ServerMemory, "_MAX_ENTRIES", 200)
    for i in range(230):
        sm.record_prompt("s", f"p{i}")
    out = sm.get_server_memory(n_events=10_000)
    assert len(out["events"]) == 200
    assert out["events"][-1]["content"] == "p229"
    assert out["events"][0]["content"] == "p30"


def test_default_cap_is_1000():
    assert sm.ServerMemory._MAX_ENTRIES == 1000


# ── reload hydration ──────────────────────────────────────────────────────────

def test_load_hydrates_session_from_db_on_reload():
    sm.record_prompt("s1", "before reload")
    sm.record_tool("s1", "vault__write")
    sm.ServerMemory._cache = []
    assert sm.get_server_memory()["events"] == []
    sm.load()
    out = sm.get_server_memory()
    assert [e["content"] for e in out["events"]] == ["before reload", "vault__write"]


# ── durability: rows persist across server runs ───────────────────────────────

def test_rows_persist_across_server_sessions(monkeypatch):
    """Different server_session_ids coexist in one DB — the point of persistence."""
    monkeypatch.setattr(sm, "SERVER_SESSION_ID", "run-A")
    sm.record_prompt("s1", "from run A")
    monkeypatch.setattr(sm, "SERVER_SESSION_ID", "run-B")
    sm.record_prompt("s1", "from run B")
    out = sm.get_server_memory()
    assert [e["content"] for e in out["events"]] == ["from run A", "from run B"]


# ── task title resolution ─────────────────────────────────────────────────────

def test_title_comes_from_the_response_not_a_task_store():
    """Title resolution is response-only.

    _title_for_task used to read titles straight out of proj_tasks.db, a store
    this repo no longer owns (task storage lives in task-framework now). This
    just confirms record_task_from_hook resolves the title from tool_response.
    """
    body = {
        "session_id": "s",
        "tool_name": "mcp__claude-hooks__tasks__set_active",
        "tool_input": {"task_id": "t1"},
        "tool_response": {"title": "from response"},
    }
    sm.record_task_from_hook(body)
    assert sm.get_server_memory()["events"][-1]["content"] == "from response"


def test_record_task_from_hook_fully_qualified_and_wrapped():
    body = {
        "session_id": "s1",
        "tool_name": "mcp__claude-hooks__tasks__set_active",
        "tool_input": {"task_id": "no-such-task-zzz"},
        "tool_response": {"content": [{"type": "text", "text": '{"task_id": "no-such-task-zzz", "title": "Do the thing"}'}]},
    }
    sm.record_task_from_hook(body)
    event = sm.get_server_memory()["events"][-1]
    assert event["ref"] == "no-such-task-zzz"
    assert event["content"] == "Do the thing"


def test_record_task_from_hook_ignores_other_mcp_tools():
    sm.record_task_from_hook({"session_id": "s1", "tool_name": "mcp__claude-hooks__tasks__finish", "tool_input": {"task_id": "abc"}})
    assert sm.get_server_memory()["events"] == []


# ── accessor endpoint ─────────────────────────────────────────────────────────

def test_session_memory_endpoint():
    import langchain_learning.session_graph as sg_mod
    from fastapi.testclient import TestClient
    from hooks.server import app

    sm.record_prompt("s1", "endpoint hello")
    sm.record_task("s1", "tX", "Endpoint task")

    sg_mod._graph = None
    with TestClient(app) as c:
        r = c.get("/session/memory", params={"n_events": 10})
    sg_mod._graph = None

    assert r.status_code == 200
    data = r.json()
    contents = [e["content"] for e in data["events"]]
    assert "endpoint hello" in contents
    assert "Endpoint task" in contents


# ── MCP wrapper ───────────────────────────────────────────────────────────────

def test_mcp_wrapper_returns_error_when_server_down():
    import src.tools.hooks as h
    with patch("src.tools.hooks.urllib.request.urlopen", side_effect=OSError("refused")):
        out = h.handle_server_memory()
    assert "error" in out


def test_mcp_wrapper_parses_server_response():
    import src.tools.hooks as h
    payload = b'{"events": [{"type": "prompt", "content": "hi"}]}'
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = payload
    with patch("src.tools.hooks.urllib.request.urlopen", return_value=cm):
        out = h.handle_server_memory(n_events=10)
    assert "| 1 | hi |" in out
