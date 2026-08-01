"""Tests for LoadActiveTaskNode — project-scoped active task suppression."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from langchain_learning.nodes.load_active_task import LoadActiveTaskNode, _project_from_cwd
from tests.fixtures.db_factories import make_tasks_db


def _make_tasks_db(tmp_path: Path, task_id="t1", tags="project:claude-hooks") -> Path:
    return make_tasks_db(tmp_path, tasks=[
        {"id": task_id, "title": "T", "status": "open", "tags": tags},
    ])


def _state(**kwargs) -> dict:
    base = {"session_id": "sess0001", "active_task_id": "", "active_task_title": "", "cwd": ""}
    base.update(kwargs)
    return base


# ── no active task ────────────────────────────────────────────────────────────

def test_noop_when_no_active_task():
    node = LoadActiveTaskNode()
    assert node(_state()) == {}


# ── no project tag ────────────────────────────────────────────────────────────

def test_passes_through_when_no_project_tag(tmp_path):
    db = _make_tasks_db(tmp_path, tags="domain:global")
    with patch("langchain_learning.nodes.load_active_task._cfg") as cfg:
        cfg.tasks_db = db
        node = LoadActiveTaskNode()
        result = node(_state(active_task_id="t1", cwd="/some/path"))
    assert result == {}


# ── project tag matches cwd ───────────────────────────────────────────────────

def test_passes_through_when_project_matches(tmp_path):
    db = _make_tasks_db(tmp_path, tags="project:myapp")
    with patch("langchain_learning.nodes.load_active_task._cfg") as cfg, \
         patch("langchain_learning.nodes.load_active_task._project_from_cwd", return_value="myapp"):
        cfg.tasks_db = db
        node = LoadActiveTaskNode()
        result = node(_state(active_task_id="t1", cwd="/workspace/myapp"))
    assert result == {}


# ── project tag mismatch → suppress ──────────────────────────────────────────

def test_no_longer_suppresses_on_project_mismatch(tmp_path):
    """Project-tag suppression is gone (task:6240c675).

    It read the task's project:<name> tag out of proj_tasks.db and blanked the
    active task when the cwd disagreed. Two reasons it went: the tag was derived
    from a working directory, which is a guess recorded as a fact, and the
    lookup read a store this repo no longer owns. task-framework scopes the
    active task by workspace path directly, so the thing this approximated is
    now answered exactly by the system that owns it.
    """
    db = _make_tasks_db(tmp_path, tags="project:myapp")
    with patch("langchain_learning.nodes.load_active_task._cfg") as cfg, \
         patch("langchain_learning.nodes.load_active_task._project_from_cwd", return_value="other-app"):
        cfg.tasks_db = db
        node = LoadActiveTaskNode()
        result = node(_state(active_task_id="t1", cwd="/workspace/other-app"))
    assert result == {}


# ── no cwd → don't suppress ──────────────────────────────────────────────────

def test_no_cwd_does_not_suppress(tmp_path):
    db = _make_tasks_db(tmp_path, tags="project:myapp")
    with patch("langchain_learning.nodes.load_active_task._cfg") as cfg:
        cfg.tasks_db = db
        node = LoadActiveTaskNode()
        result = node(_state(active_task_id="t1", cwd=""))
    assert result == {}


# ── _project_from_cwd ─────────────────────────────────────────────────────────

def test_project_from_cwd_reads_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "testpkg"\n')
    result = _project_from_cwd(str(tmp_path))
    assert result == "testpkg"


def test_project_from_cwd_returns_none_when_no_pyproject(tmp_path):
    result = _project_from_cwd(str(tmp_path))
    assert result is None
