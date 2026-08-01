"""Tests for hooks/session_state.py — get_current_session, get_active_session,
and _latest_checkpoint_tuple (the cross-thread "most recent" lookup)."""
from unittest.mock import MagicMock, patch

from hooks.session_state import get_active_session, get_current_session


def _make_checkpoint_tuple(thread_id: str, turn: int, ts: str, active_task_id: str = ""):
    tup = MagicMock()
    channel_values = {"turn": turn}
    if active_task_id:
        channel_values["active_task_id"] = active_task_id
        channel_values["active_task_title"] = f"Title for {active_task_id}"
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
        # Unlike get_active_session, a checkpoint with no active_task_id still
        # yields a session_id — that's the whole point of this helper.
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


class TestGetActiveSession:
    def test_returns_active_task_from_most_recent_thread(self):
        old_thread = _make_checkpoint_tuple(
            "old-session", turn=1, ts="2020-01-01T00:00:00+00:00", active_task_id="task-old"
        )
        new_thread = _make_checkpoint_tuple(
            "new-session", turn=3, ts="2026-07-28T01:30:00+00:00", active_task_id="task-new"
        )
        with _mock_checkpointer({"old-session": old_thread, "new-session": new_thread}):
            result = get_active_session()
        assert result["task_id"] == "task-new"
        assert result["session_id"] == "new-session"

    def test_no_active_task_returns_empty(self):
        tup = _make_checkpoint_tuple("sess-abc", 0, ts="2026-07-28T01:00:00+00:00")
        with _mock_checkpointer({"sess-abc": tup}):
            result = get_active_session()
        assert result == {}

    def test_a_finished_task_is_still_reported_from_the_checkpoint(self):
        """This function reports the checkpoint, not the truth.

        It used to resolve the id against proj_tasks.db and drop done and
        abandoned tasks. That made it look authoritative about a fact this repo
        no longer owns — task-framework holds the active task and is the only
        thing that can say whether one is still open. Filtering here would mean
        a second active-task pointer with nothing to reconcile it against, so
        a stale checkpoint now reports what it holds and the caller resolves it.
        """
        tup = _make_checkpoint_tuple("sess-abc", 1, ts="2026-07-28T01:00:00+00:00", active_task_id="task-1")
        with _mock_checkpointer({"sess-abc": tup}):
            result = get_active_session()
        assert result["task_id"] == "task-1"

    # test_no_task_store_is_consulted removed here (task:87ec7876). It proved
    # get_active_session never calls src.tools.tasks.handle_get by patching that
    # target and asserting it wasn't hit. The module is gone, so the patch
    # target no longer exists — the property is now true unconditionally, and
    # the test above already covers the behavior that matters.

    def test_no_checkpointer_returns_empty(self):
        graph = MagicMock()
        graph.checkpointer = None
        with patch("langchain_learning.session_graph._graph", graph):
            result = get_active_session()
        assert result == {}
