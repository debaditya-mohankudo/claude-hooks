"""Tests for hooks/session_state.py — get_current_session and
_latest_checkpoint_tuple (the cross-thread "most recent" lookup).

get_active_session was removed (task:8529435a) — active_task_id/
active_task_title stopped being written into checkpoint state once
task:882d67fa moved active-task ownership fully to task-framework, so it
always returned {}. Ask task-framework (tasks__active) for the live answer.
"""
from unittest.mock import MagicMock, patch

import hooks.session_state as session_state
from hooks.session_state import get_active_task, get_current_session, set_active_task


def _make_checkpoint_tuple(thread_id: str, turn: int, ts: str):
    tup = MagicMock()
    channel_values = {"turn": turn}
    tup.checkpoint = {"channel_values": channel_values, "ts": ts}
    tup.config = {"configurable": {"thread_id": thread_id}}
    return tup


def _mock_checkpointer(thread_tuples: dict):
    """thread_tuples: {thread_id: CheckpointTuple}. Mocks .storage (for thread
    enumeration) and .get_tuple (for the per-thread latest lookup) — the two
    calls _latest_checkpoint_tuple actually uses, replacing the old .list(None)
    approach that silently only ever inspected one thread."""
    checkpointer = MagicMock()
    checkpointer.storage = {tid: {} for tid in thread_tuples}

    def _get_tuple(config):
        tid = config["configurable"]["thread_id"]
        return thread_tuples.get(tid)

    checkpointer.get_tuple.side_effect = _get_tuple
    graph = MagicMock()
    graph.checkpointer = checkpointer
    return patch("langchain_learning.session_graph._graph", graph)


class TestGetCurrentSession:
    def test_returns_session_id_and_turn(self):
        tup = _make_checkpoint_tuple("sess-abc", 7, ts="2026-07-28T01:00:00+00:00")
        with _mock_checkpointer({"sess-abc": tup}):
            result = get_current_session()
        assert result == {"session_id": "sess-abc", "turn": 7}

    def test_no_checkpoint_returns_empty(self):
        with _mock_checkpointer({}):
            result = get_current_session()
        assert result == {}

    def test_no_checkpointer_returns_empty(self):
        graph = MagicMock()
        graph.checkpointer = None
        with patch("langchain_learning.session_graph._graph", graph):
            result = get_current_session()
        assert result == {}

    def test_does_not_require_active_task(self):
        # No active-task concept lives here at all — a bare checkpoint still
        # yields a session_id, which is the whole point of this helper.
        tup = _make_checkpoint_tuple("sess-xyz", 0, ts="2026-07-28T01:00:00+00:00")
        with _mock_checkpointer({"sess-xyz": tup}):
            result = get_current_session()
        assert result["session_id"] == "sess-xyz"

    def test_exception_returns_empty(self):
        graph = MagicMock()
        checkpointer = MagicMock()
        # storage access itself raises — exercises the outer try/except in get_current_session
        type(checkpointer).storage = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        graph.checkpointer = checkpointer
        with patch("langchain_learning.session_graph._graph", graph):
            result = get_current_session()
        assert result == {}

    def test_picks_globally_most_recent_thread_not_first_in_storage(self):
        """Regression test for the bug that made hooks__session_id/get_current_session
        permanently stuck on a long-lived stale thread: MemorySaver.list(None) without
        a config iterates checkpointer.storage's thread_ids in dict-insertion order and
        yields that FIRST thread's own history before ever considering other threads, so
        next(iter(checkpointer.list(None))) silently ignored every newer session. An old
        thread inserted first (e.g. a long-lived integration-test session) must NOT win
        over a genuinely more-recent thread inserted later — the fix compares checkpoint
        timestamps across all threads instead of trusting storage iteration order."""
        old_thread = _make_checkpoint_tuple(
            "old-integration-test-session", turn=99, ts="2020-01-01T00:00:00+00:00"
        )
        new_thread = _make_checkpoint_tuple(
            "new-real-session", turn=2, ts="2026-07-28T01:30:00+00:00"
        )
        # old_thread inserted first into storage — this is exactly the scenario
        # that broke the old next(iter(...)) approach.
        with _mock_checkpointer({"old-integration-test-session": old_thread, "new-real-session": new_thread}):
            result = get_current_session()
        assert result["session_id"] == "new-real-session"
        assert result["turn"] == 2

    def test_returns_recent_thread_even_when_it_sorts_later_alphabetically(self):
        # Guards against accidentally sorting by thread_id string instead of timestamp.
        a_thread = _make_checkpoint_tuple("aaa-old", turn=1, ts="2020-01-01T00:00:00+00:00")
        z_thread = _make_checkpoint_tuple("zzz-new", turn=5, ts="2026-07-28T01:30:00+00:00")
        with _mock_checkpointer({"aaa-old": a_thread, "zzz-new": z_thread}):
            result = get_current_session()
        assert result["session_id"] == "zzz-new"


class TestActiveTaskPush:
    """set_active_task/get_active_task (task:996cc8f0) — the in-memory cache
    backing POST /set-active-taskid and GET /session/active-task."""

    def setup_method(self):
        session_state._active_task_by_workspace.clear()

    def teardown_method(self):
        session_state._active_task_by_workspace.clear()

    def test_set_then_get_roundtrips(self):
        set_active_task("/repo/a", "task-1", "Do the thing")
        result = get_active_task("/repo/a")
        assert result["workspace"] == "/repo/a"
        assert result["task_id"] == "task-1"
        assert result["title"] == "Do the thing"
        assert "ts" in result

    def test_unknown_workspace_returns_empty(self):
        assert get_active_task("/repo/never-set") == {}

    def test_empty_workspace_returns_empty(self):
        assert get_active_task("") == {}

    def test_empty_task_id_clears_existing_entry(self):
        set_active_task("/repo/b", "task-1")
        set_active_task("/repo/b", "")
        assert get_active_task("/repo/b") == {}

    def test_set_with_no_workspace_is_a_noop(self):
        set_active_task("", "task-1")
        assert session_state._active_task_by_workspace == {}

    def test_workspaces_are_independent(self):
        set_active_task("/repo/a", "task-1")
        set_active_task("/repo/b", "task-2")
        assert get_active_task("/repo/a")["task_id"] == "task-1"
        assert get_active_task("/repo/b")["task_id"] == "task-2"

    def test_setting_again_overwrites_previous_value(self):
        set_active_task("/repo/a", "task-1", "First")
        set_active_task("/repo/a", "task-2", "Second")
        result = get_active_task("/repo/a")
        assert result["task_id"] == "task-2"
        assert result["title"] == "Second"
