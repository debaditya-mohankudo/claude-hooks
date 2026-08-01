"""Tests for LoadActiveTaskNode — a pass-through log now that project-tag
suppression is gone (task:87ec7876). The node no longer reads any database:
it returns {} unconditionally, whether or not a task is active.
"""
from __future__ import annotations

from langchain_learning.nodes.load_active_task import LoadActiveTaskNode


def _state(**kwargs) -> dict:
    base = {"session_id": "sess0001", "active_task_id": "", "active_task_title": "", "cwd": ""}
    base.update(kwargs)
    return base


def test_noop_when_no_active_task():
    node = LoadActiveTaskNode()
    assert node(_state()) == {}


def test_noop_when_task_is_active():
    """No suppression left to apply — an active task always passes through."""
    node = LoadActiveTaskNode()
    result = node(_state(active_task_id="t1", active_task_title="Fix bug", cwd="/workspace/other-app"))
    assert result == {}
