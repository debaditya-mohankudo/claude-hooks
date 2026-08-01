"""Tests for BackfillMemoryFilesNode and its helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from langchain_learning.nodes.backfill_memory_files import (
    BackfillMemoryFilesNode,
    _run_backfill,
    _parse_files_section,
    _file_tokens,
)
from tests.fixtures.db_factories import make_memory_db


def _state(**kwargs) -> dict:
    base = {"session_id": "sess1234", "task_files": [], "active_task_domain": ""}
    base.update(kwargs)
    return base


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
    assert "hook" not in tokens and "hooks" not in tokens


def test_file_tokens_empty():
    assert _file_tokens([]) == set()


# ── _run_backfill ─────────────────────────────────────────────────────────────

def test_run_backfill_updates_matching_memory(tmp_path):
    db = make_memory_db(tmp_path, [
        {"name": "claude-hooks-gate-framework", "domain": "claude-hooks", "tags": "gate, gates, hooks"},
    ])
    with patch("langchain_learning.nodes.backfill_memory_files._cfg") as cfg:
        cfg.memory_db = db
        count = _run_backfill("claude-hooks", ["hooks/gates.py"])
    assert count == 1
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute("SELECT files FROM memories WHERE name='claude-hooks-gate-framework'").fetchone()
    assert row[0] == "hooks/gates.py"


def test_run_backfill_skips_already_filled(tmp_path):
    db = make_memory_db(tmp_path, [
        {"name": "claude-hooks-gate-framework", "domain": "claude-hooks",
         "tags": "gate gates", "files": "hooks/gates.py"},
    ])
    with patch("langchain_learning.nodes.backfill_memory_files._cfg") as cfg:
        cfg.memory_db = db
        count = _run_backfill("claude-hooks", ["hooks/gates.py"])
    assert count == 0


def test_run_backfill_no_overlap_skips(tmp_path):
    db = make_memory_db(tmp_path, [
        {"name": "claude-hooks-unrelated", "domain": "claude-hooks", "tags": "auth login"},
    ])
    with patch("langchain_learning.nodes.backfill_memory_files._cfg") as cfg:
        cfg.memory_db = db
        count = _run_backfill("claude-hooks", ["hooks/gates.py"])
    assert count == 0


def test_run_backfill_missing_db_returns_zero(tmp_path):
    with patch("langchain_learning.nodes.backfill_memory_files._cfg") as cfg:
        cfg.memory_db = tmp_path / "nonexistent.sqlite"
        count = _run_backfill("claude-hooks", ["hooks/gates.py"])
    assert count == 0


def test_run_backfill_empty_files_returns_zero(tmp_path):
    db = make_memory_db(tmp_path, [
        {"name": "some-memory", "domain": "claude-hooks", "tags": "gate"},
    ])
    with patch("langchain_learning.nodes.backfill_memory_files._cfg") as cfg:
        cfg.memory_db = db
        count = _run_backfill("claude-hooks", [])
    assert count == 0


# ── BackfillMemoryFilesNode ───────────────────────────────────────────────────

def test_node_skips_replay_session(tmp_path):
    db = make_memory_db(tmp_path, [
        {"name": "claude-hooks-gate-framework", "domain": "claude-hooks", "tags": "gate gates"},
    ])
    with patch("langchain_learning.nodes.backfill_memory_files._cfg") as cfg:
        cfg.memory_db = db
        node = BackfillMemoryFilesNode()
        result = node(_state(
            session_id="replay-abc123",
            task_files=["hooks/gates.py"],
            active_task_domain="claude-hooks",
        ))
    assert result == {"backfill_count": 0}
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute("SELECT files FROM memories WHERE name='claude-hooks-gate-framework'").fetchone()
    assert row[0] is None


def test_node_skips_when_no_task_files():
    node = BackfillMemoryFilesNode()
    result = node(_state(task_files=[], active_task_domain="claude-hooks"))
    assert result == {"backfill_count": 0}


def test_node_skips_when_no_domain():
    node = BackfillMemoryFilesNode()
    result = node(_state(task_files=["hooks/gates.py"], active_task_domain=""))
    assert result == {"backfill_count": 0}


def test_node_backfills_and_returns_count(tmp_path):
    db = make_memory_db(tmp_path, [
        {"name": "claude-hooks-gate-framework", "domain": "claude-hooks", "tags": "gate gates hooks"},
    ])
    with patch("langchain_learning.nodes.backfill_memory_files._cfg") as cfg:
        cfg.memory_db = db
        node = BackfillMemoryFilesNode()
        result = node(_state(
            task_files=["hooks/gates.py"],
            active_task_domain="claude-hooks",
        ))
    assert result == {"backfill_count": 1}
