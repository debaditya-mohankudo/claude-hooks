"""Tests for LoadTaskCodeNode — TurboVec semantic code search."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from langchain_learning.nodes.load_task_code import LoadTaskCodeNode


def _state(**kwargs) -> dict:
    base = {
        "active_task_id": "abc12345",
        "active_task_title": "Fix import bug",
        "cwd": "",
        "session_id": "test",
    }
    base.update(kwargs)
    return base


def test_no_active_task_returns_empty():
    node = LoadTaskCodeNode()
    result = node(_state(active_task_id="", active_task_title=""))
    assert result == {"task_rag_chunks": []}


def test_missing_index_returns_empty(tmp_path):
    node = LoadTaskCodeNode()
    result = node(_state(cwd=str(tmp_path)))  # no .code_embeddings.tvim present
    assert result == {"task_rag_chunks": []}


def test_returns_chunks_from_query_tvim(tmp_path):
    # Create a dummy tvim file so the existence check passes
    (tmp_path / ".code_embeddings.tvim").write_bytes(b"")
    (tmp_path / ".code_embeddings.meta.json").write_text("{}")

    chunks = [
        {"name": "handle_neighbors", "file": "src/tools/tasks.py", "kind": "function", "line": 773, "score": 0.9},
        {"name": "load_index",       "file": "src/tools/rag_core.py", "kind": "function", "line": 10,  "score": 0.8},
    ]

    with patch("langchain_learning.nodes.load_task_code._query_tvim", return_value=chunks) as mock_q:
        node = LoadTaskCodeNode()
        result = node(_state(cwd=str(tmp_path)))

    mock_q.assert_called_once()
    assert result["task_rag_chunks"] == chunks


def test_query_tvim_error_returns_empty(tmp_path):
    (tmp_path / ".code_embeddings.tvim").write_bytes(b"")
    (tmp_path / ".code_embeddings.meta.json").write_text("{}")

    with patch("langchain_learning.nodes.load_task_code._query_tvim", side_effect=Exception("ollama down")):
        node = LoadTaskCodeNode()
        result = node(_state(cwd=str(tmp_path)))

    assert result == {"task_rag_chunks": []}


def test_fallback_to_repo_root_when_no_cwd(tmp_path):
    # When cwd is empty, the node uses _TVIM_PATH (repo root). If that doesn't
    # exist in test env, it returns empty without calling _query_tvim.
    with patch("langchain_learning.nodes.load_task_code._TVIM_PATH", tmp_path / "nonexistent.tvim"):
        node = LoadTaskCodeNode()
        result = node(_state(cwd=""))

    assert result == {"task_rag_chunks": []}


def test_query_tvim_called_with_correct_args(tmp_path):
    (tmp_path / ".code_embeddings.tvim").write_bytes(b"")
    (tmp_path / ".code_embeddings.meta.json").write_text("{}")

    with patch("langchain_learning.nodes.load_task_code._query_tvim", return_value=[]) as mock_q:
        node = LoadTaskCodeNode()
        node(_state(active_task_title="Fix import bug", cwd=str(tmp_path)))

    args = mock_q.call_args
    assert args[0][0] == "Fix import bug"   # query
    assert args[0][1] == 3                  # top-k
