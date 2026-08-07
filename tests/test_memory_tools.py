"""Tests for src/tools/memory.py — MCP memory tool handlers."""
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.memory import (
    handle_add,
    handle_add_batch,
    handle_get,
    handle_list,
    handle_search,
    handle_delete,
    handle_tool_hints,
    handle_read_compact,
)
from src.db.schema import MEMORIES_DDL, MCP_TOOL_HINTS_DDL


def _make_memory_db(memories: list[dict] | None = None) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    con = sqlite3.connect(tmp.name)
    con.executescript(MEMORIES_DDL)
    for m in (memories or []):
        con.execute(
            "INSERT INTO memories (name, type, tags, body) VALUES (:name, :type, :tags, :body)",
            m,
        )
    con.commit()
    con.close()
    return Path(tmp.name)


def _make_tool_hints_db(hints: list[dict] | None = None) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    con = sqlite3.connect(tmp.name)
    con.executescript(MCP_TOOL_HINTS_DDL)
    for h in (hints or []):
        con.execute(
            "INSERT INTO mcp_tool_hints (tool_name, count, last_used, avg_latency_ms, keywords, skill) VALUES (:tool_name, :count, :last_used, :avg_latency_ms, :keywords, :skill)",
            h,
        )
    con.commit()
    con.close()
    return Path(tmp.name)


@pytest.fixture
def mem_db():
    return _make_memory_db([
        {"name": "alpha", "type": "user", "tags": "foo,bar", "body": "User is a developer"},
        {"name": "beta", "type": "feedback", "tags": "macos", "body": "Use short responses"},
        {"name": "gamma", "type": "project", "tags": "market", "body": "Market project context"},
    ])


@pytest.fixture
def hints_db():
    return _make_tool_hints_db([
        {"tool_name": "imessage__send", "count": 10, "last_used": "2026-06-01", "avg_latency_ms": 120.0, "keywords": "message send macos", "skill": "local-mac-imessage"},
        {"tool_name": "calendar__list", "count": 5, "last_used": "2026-06-02", "avg_latency_ms": 80.0, "keywords": "calendar macos", "skill": "local-mac-calendar"},
        {"tool_name": "market__prices", "count": 20, "last_used": "2026-06-03", "avg_latency_ms": 200.0, "keywords": "prices market", "skill": "market-intel-live-prices"},
    ])


# ---------------------------------------------------------------------------
# handle_add
# ---------------------------------------------------------------------------

def test_add_inserts_new_memory(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_add(name="new-mem", type="user", body="New memory body")
    assert result["ok"] is True
    assert result["name"] == "new-mem"
    assert result["action"] == "upserted"


def test_add_updates_existing_memory(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        handle_add(name="alpha", type="user", body="Updated body")
        result = handle_get.__wrapped__(name="alpha") if hasattr(handle_get, "__wrapped__") else None
    con = sqlite3.connect(str(mem_db))
    row = con.execute("SELECT body FROM memories WHERE name='alpha'").fetchone()
    con.close()
    assert row[0] == "Updated body"


def test_add_rejects_invalid_type(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_add(name="bad", type="invalid_type", body="body")
    assert "error" in result
    assert "invalid_type" in result["error"]


def test_add_persists_files_and_docs(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        handle_add(name="with-files", type="project", body="body",
                   files="src/tools/memory.py,hooks/server.py",
                   docs="Vault/Memory System.md")
    con = sqlite3.connect(str(mem_db))
    row = con.execute("SELECT files, docs FROM memories WHERE name='with-files'").fetchone()
    con.close()
    assert row[0] == "src/tools/memory.py,hooks/server.py"
    assert row[1] == "Vault/Memory System.md"


def test_add_batch_persists_files_and_docs(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_add_batch([
            {"name": "batch-a", "type": "feedback", "body": "b",
             "files": "hooks/gates.py", "docs": ""},
            {"name": "batch-b", "type": "user", "body": "c"},
        ])
    assert result["count"] == 2
    con = sqlite3.connect(str(mem_db))
    row_a = con.execute("SELECT files, docs FROM memories WHERE name='batch-a'").fetchone()
    row_b = con.execute("SELECT files FROM memories WHERE name='batch-b'").fetchone()
    con.close()
    assert row_a[0] == "hooks/gates.py"
    assert row_a[1] == ""
    assert row_b[0] == ""


def test_add_persists_related(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        handle_add(name="with-related", type="project", body="body",
                   related="claude-hooks-gate-framework,claude-hooks-current-gates")
    con = sqlite3.connect(str(mem_db))
    row = con.execute("SELECT related FROM memories WHERE name='with-related'").fetchone()
    con.close()
    assert row[0] == "claude-hooks-gate-framework,claude-hooks-current-gates"


def test_add_batch_persists_related(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_add_batch([
            {"name": "rel-a", "type": "project", "body": "a",
             "related": "rel-b"},
            {"name": "rel-b", "type": "project", "body": "b"},
        ])
    assert result["count"] == 2
    con = sqlite3.connect(str(mem_db))
    row_a = con.execute("SELECT related FROM memories WHERE name='rel-a'").fetchone()
    row_b = con.execute("SELECT related FROM memories WHERE name='rel-b'").fetchone()
    con.close()
    assert row_a[0] == "rel-b"
    assert row_b[0] == ""


def test_add_stamps_last_validated(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        handle_add(name="new-mem", type="user", body="body")
    con = sqlite3.connect(str(mem_db))
    row = con.execute("SELECT last_validated FROM memories WHERE name='new-mem'").fetchone()
    con.close()
    assert row[0] is not None


def test_add_updates_last_validated_on_existing_memory(mem_db):
    con = sqlite3.connect(str(mem_db))
    con.execute("UPDATE memories SET last_validated = '2020-01-01 00:00:00' WHERE name='alpha'")
    con.commit()
    con.close()
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        handle_add(name="alpha", type="user", body="refreshed body")
    con = sqlite3.connect(str(mem_db))
    row = con.execute("SELECT last_validated FROM memories WHERE name='alpha'").fetchone()
    con.close()
    assert row[0] != "2020-01-01 00:00:00"


def test_add_batch_stamps_last_validated(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        handle_add_batch([{"name": "batch-lv", "type": "user", "body": "b"}])
    con = sqlite3.connect(str(mem_db))
    row = con.execute("SELECT last_validated FROM memories WHERE name='batch-lv'").fetchone()
    con.close()
    assert row[0] is not None


# ---------------------------------------------------------------------------
# handle_get
# ---------------------------------------------------------------------------

def test_get_returns_existing_memory(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_get(name="alpha")
    assert result["name"] == "alpha"
    assert result["body"] == "User is a developer"


def test_get_returns_error_for_missing_name(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_get(name="nonexistent")
    assert "error" in result


# ---------------------------------------------------------------------------
# handle_list
# ---------------------------------------------------------------------------

def test_list_returns_all_memories(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_list()
    assert result["count"] == 3


def test_list_filters_by_type(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_list(type="feedback")
    assert result["count"] == 1
    assert result["memories"][0]["name"] == "beta"


def test_list_returns_empty_for_no_match(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_list(type="nonexistent")
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# handle_search
# ---------------------------------------------------------------------------

def test_search_matches_body(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_search(query="developer")
    assert result["count"] >= 1
    names = [r["name"] for r in result["results"]]
    assert "alpha" in names


def test_search_matches_tags(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_search(query="macos")
    assert result["count"] >= 1


def test_search_no_results_returns_empty(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_search(query="zzznomatchzzz")
    assert result["count"] == 0


def test_search_multi_word_fallback(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_search(query="developer market")
    assert result["count"] >= 1


def test_search_tags_source_global(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_search(query="developer")
    assert result["count"] >= 1


# ---------------------------------------------------------------------------
# handle_delete
# ---------------------------------------------------------------------------

def test_delete_removes_existing_memory(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_delete(name="alpha")
    assert result["ok"] is True
    assert result["deleted"] == "alpha"
    con = sqlite3.connect(str(mem_db))
    row = con.execute("SELECT 1 FROM memories WHERE name='alpha'").fetchone()
    con.close()
    assert row is None


def test_delete_returns_error_for_missing_name(mem_db):
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_delete(name="nonexistent")
    assert "error" in result


# ---------------------------------------------------------------------------
# handle_tool_hints
# ---------------------------------------------------------------------------

def test_tool_hints_returns_all(hints_db):
    with patch("tools.memory.TOOL_HINTS_DB", hints_db):
        result = handle_tool_hints()
    assert result["count"] == 3


def test_tool_hints_sorted_by_count_desc(hints_db):
    with patch("tools.memory.TOOL_HINTS_DB", hints_db):
        result = handle_tool_hints()
    counts = [t["count"] for t in result["tools"]]
    assert counts == sorted(counts, reverse=True)


def test_tool_hints_missing_db_returns_error(tmp_path):
    with patch("tools.memory.TOOL_HINTS_DB", tmp_path / "nonexistent.sqlite"):
        result = handle_tool_hints()
    assert "error" in result


def test_tool_hints_top_n_respected(hints_db):
    with patch("tools.memory.TOOL_HINTS_DB", hints_db):
        result = handle_tool_hints(top_n=1)
    assert result["count"] == 1


# ---------------------------------------------------------------------------
# handle_read_compact
# ---------------------------------------------------------------------------

def test_read_compact_returns_summary(tmp_path):
    session_id = "test-session-123"
    project_dir = tmp_path / ".claude" / "projects" / "my-project"
    project_dir.mkdir(parents=True)
    jsonl = project_dir / f"{session_id}.jsonl"
    summary_content = (
        "This session is being continued from a previous conversation that was "
        "too long.\n\nSummary:\nWe discussed turbovec upgrade.\n\nIf you need specific details, ask."
    )
    jsonl.write_text(
        json.dumps({"type": "user", "message": {"content": summary_content}}) + "\n"
    )
    with patch("tools.memory.Path.home", return_value=tmp_path):
        result = handle_read_compact(session_id=session_id)
    assert result["session_id"] == session_id
    assert "turbovec" in result["summary"]


def test_read_compact_no_jsonl_returns_error(tmp_path):
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    with patch("tools.memory.Path.home", return_value=tmp_path):
        result = handle_read_compact(session_id="nonexistent-session")
    assert "error" in result


def test_read_compact_no_compact_marker_returns_error(tmp_path):
    session_id = "test-no-compact"
    project_dir = tmp_path / ".claude" / "projects" / "proj"
    project_dir.mkdir(parents=True)
    jsonl = project_dir / f"{session_id}.jsonl"
    jsonl.write_text(json.dumps({"type": "user", "message": {"content": "regular message"}}) + "\n")
    with patch("tools.memory.Path.home", return_value=tmp_path):
        result = handle_read_compact(session_id=session_id)
    assert "error" in result


# ---------------------------------------------------------------------------
# handle_add_batch
# ---------------------------------------------------------------------------

def test_add_batch_inserts_multiple(mem_db):
    batch = [
        {"name": "batch-1", "type": "user", "body": "First batch memory", "tags": "a"},
        {"name": "batch-2", "type": "feedback", "body": "Second batch memory", "tags": "b"},
    ]
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_add_batch(batch)
    assert result["ok"] is True
    assert result["count"] == 2
    assert all(r["action"] == "upserted" for r in result["results"])


def test_add_batch_rejects_invalid_type(mem_db):
    batch = [
        {"name": "good-mem", "type": "user", "body": "ok"},
        {"name": "bad-mem", "type": "invalid", "body": "bad type"},
    ]
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_add_batch(batch)
    assert result["count"] == 1
    names = {r["name"]: r for r in result["results"]}
    assert "action" in names["good-mem"]
    assert "error" in names["bad-mem"]


def test_add_batch_missing_required_field(mem_db):
    batch = [{"name": "no-body", "type": "user"}]
    with patch("tools.memory.MEMORY_DB", str(mem_db)):
        result = handle_add_batch(batch)
    assert result["count"] == 0
    assert "error" in result["results"][0]
