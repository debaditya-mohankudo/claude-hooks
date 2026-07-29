"""Tests for ActivateTaskNode — PostToolUse bridge for task activation."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from langchain_learning.nodes.activate_task import (
    ActivateTaskNode,
    _build_execution_contract,
    _lookup_task,
    _score_memories,
)
from langchain_learning.nodes.backfill_memory_files import (
    _parse_files_section,
    _file_tokens,
)
from tests.fixtures.db_factories import make_tasks_db


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_tasks_db(tmp_path: Path) -> Path:
    db = make_tasks_db(tmp_path, tasks=[
        {"id": "task01", "title": "Fix auth bug", "body": "body text", "status": "open"},
    ])
    return db


def _state(**kwargs) -> dict:
    base = {"session_id": "sess1234", "tool_name": "", "tool_input": {}, "task_stack": []}
    base.update(kwargs)
    return base


# ── no-op for unrelated tools ─────────────────────────────────────────────────

def test_noop_for_unrelated_tool():
    node = ActivateTaskNode()
    result = node(_state(tool_name="tasks__list"))
    assert result == {}


def test_noop_for_empty_tool():
    node = ActivateTaskNode()
    result = node(_state(tool_name=""))
    assert result == {}


# ── tasks__set_active ─────────────────────────────────────────────────────────

def test_set_active_missing_task_id():
    node = ActivateTaskNode()
    result = node(_state(tool_name="tasks__set_active", tool_input={}))
    assert result == {}


def test_set_active_activates_task(tmp_path):
    db = _make_tasks_db(tmp_path)
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        cfg.memory_db = tmp_path / "MEMORY.sqlite"  # doesn't exist → empty memories
        node = ActivateTaskNode()
        result = node(_state(
            tool_name="tasks__set_active",
            tool_input={"task_id": "task01"},
        ))
    assert result["active_task_id"] == "task01"
    assert result["active_task_title"] == "Fix auth bug"
    assert result["task_stack"] == []


def test_set_active_pushes_existing_onto_stack(tmp_path):
    db = _make_tasks_db(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("INSERT INTO open_tasks (id, title, body, status) VALUES ('task02', 'New task', '', 'open')")
        conn.commit()
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        cfg.memory_db = tmp_path / "MEMORY.sqlite"
        node = ActivateTaskNode()
        result = node(_state(
            tool_name="tasks__set_active",
            tool_input={"task_id": "task02"},
            active_task_id="task01",
            task_stack=[],
        ))
    assert result["active_task_id"] == "task02"
    assert "task01" in result["task_stack"]


def test_set_active_task_not_found(tmp_path):
    db = _make_tasks_db(tmp_path)
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        cfg.memory_db = tmp_path / "MEMORY.sqlite"
        node = ActivateTaskNode()
        result = node(_state(
            tool_name="tasks__set_active",
            tool_input={"task_id": "nonexistent"},
        ))
    # {} means the node ran and found no matching task — not a silent failure
    assert result == {}
    # Verify the existing task was NOT disturbed
    import sqlite3
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute("SELECT status FROM open_tasks WHERE id='task01'").fetchone()
    assert row[0] == "open"


# ── tasks__pop_active ─────────────────────────────────────────────────────────

def test_pop_empty_stack_clears_active():
    node = ActivateTaskNode()
    result = node(_state(tool_name="tasks__pop_active", task_stack=[]))
    assert result["active_task_id"] == ""
    assert result["task_stack"] == []


def test_pop_restores_previous_task(tmp_path):
    db = _make_tasks_db(tmp_path)
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        cfg.memory_db = tmp_path / "MEMORY.sqlite"
        node = ActivateTaskNode()
        result = node(_state(
            tool_name="tasks__pop_active",
            task_stack=["task01"],
        ))
    assert result["active_task_id"] == "task01"
    assert result["task_stack"] == []


# ── _lookup_task ──────────────────────────────────────────────────────────────

def test_lookup_task_returns_none_when_db_missing(tmp_path):
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = tmp_path / "missing.db"
        assert _lookup_task("task01") is None


def test_lookup_task_returns_none_for_unknown(tmp_path):
    db = _make_tasks_db(tmp_path)
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        assert _lookup_task("nope") is None


def test_lookup_task_returns_title_body(tmp_path):
    db = _make_tasks_db(tmp_path)
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        result = _lookup_task("task01")
    assert result == ("Fix auth bug", "body text", "", "")


# ── _parse_files_section ──────────────────────────────────────────────────────

def test_parse_files_section_comma_separated():
    body = "Type: feature\nTask:\ndesc\n\nFiles:\nhooks/gates.py, src/tools/memory.py\n\nResolution:\n"
    assert _parse_files_section(body) == ["hooks/gates.py", "src/tools/memory.py"]


def test_parse_files_section_newline_separated():
    body = "Type: feature\nTask:\ndesc\n\nFiles:\nhooks/gates.py\nsrc/tools/memory.py\n"
    result = _parse_files_section(body)
    assert "hooks/gates.py" in result
    assert "src/tools/memory.py" in result


def test_parse_files_section_missing():
    body = "Type: feature\nTask:\ndesc\n\nResolution:\nnone"
    assert _parse_files_section(body) == []


# ── _file_tokens ──────────────────────────────────────────────────────────────

def test_file_tokens_extracts_stem():
    tokens = _file_tokens(["hooks/gates.py"])
    assert "gate" in tokens or "gates" in tokens
    # directory components are intentionally excluded — too generic
    assert "hook" not in tokens and "hooks" not in tokens


def test_file_tokens_empty():
    assert _file_tokens([]) == set()


def test_activate_emits_task_files_and_domain(tmp_path):
    db = _make_tasks_db(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE open_tasks SET body=? WHERE id='task01'",
            ("Type: feature\nTask:\ndesc\n\nFiles:\nhooks/gates.py\n",),
        )
        conn.commit()
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        cfg.memory_db = tmp_path / "MEMORY.sqlite"
        node = ActivateTaskNode()
        result = node(_state(
            tool_name="tasks__set_active",
            tool_input={"task_id": "task01"},
        ))
    assert result.get("task_files") == ["hooks/gates.py"]
    assert result.get("active_task_domain") != ""


# ── execution_contract ──────────────────────────────────────────────────────

def test_set_active_emits_execution_contract(tmp_path):
    db = _make_tasks_db(tmp_path)
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        cfg.memory_db = tmp_path / "MEMORY.sqlite"
        node = ActivateTaskNode()
        result = node(_state(
            tool_name="tasks__set_active",
            tool_input={"task_id": "task01"},
        ))
    assert "task:task01" in result["execution_contract"]
    assert "Fix auth bug" in result["execution_contract"]


def test_execution_contract_identical_across_repeated_activation_calls(tmp_path):
    db = _make_tasks_db(tmp_path)
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        cfg.memory_db = tmp_path / "MEMORY.sqlite"
        node = ActivateTaskNode()
        first = node(_state(tool_name="tasks__set_active", tool_input={"task_id": "task01"}))
        second = node(_state(tool_name="tasks__set_active", tool_input={"task_id": "task01"}))
    assert first["execution_contract"] == second["execution_contract"]


def test_execution_contract_cleared_on_pop_to_empty_stack():
    node = ActivateTaskNode()
    result = node(_state(tool_name="tasks__pop_active", task_stack=[]))
    assert result["execution_contract"] == ""


def test_build_execution_contract_contains_finish_philosophy():
    contract = _build_execution_contract("abc123", "Some task")
    assert "task:abc123" in contract
    assert "Some task" in contract
    assert "Finish decisively" in contract


def test_no_cwd_falls_back_to_global_scoring(tmp_path):
    """No cwd in state → global _score_memories path runs as normal."""
    db = _make_tasks_db(tmp_path)
    with patch("langchain_learning.nodes.activate_task._cfg") as cfg:
        cfg.tasks_db = db
        cfg.memory_db = tmp_path / "MEMORY.sqlite"
        node = ActivateTaskNode()
        result = node(_state(tool_name="tasks__set_active", tool_input={"task_id": "task01"}))
    assert result["task_memories"] == []  # empty since MEMORY.sqlite doesn't exist here
