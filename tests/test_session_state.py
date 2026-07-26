"""Tests for hooks/session_state.py — get_current_session."""
from unittest.mock import MagicMock, patch

from hooks.session_state import get_current_session


def _mock_checkpointer(latest):
    checkpointer = MagicMock()
    checkpointer.list.return_value = iter([latest] if latest else [])
    graph = MagicMock()
    graph.checkpointer = checkpointer
    return patch("langchain_learning.session_graph._graph", graph)


def _make_checkpoint_tuple(thread_id: str, turn: int):
    tup = MagicMock()
    tup.checkpoint = {"channel_values": {"turn": turn}}
    tup.config = {"configurable": {"thread_id": thread_id}}
    return tup


class TestGetCurrentSession:
    def test_returns_session_id_and_turn(self):
        tup = _make_checkpoint_tuple("sess-abc", 7)
        with _mock_checkpointer(tup):
            result = get_current_session()
        assert result == {"session_id": "sess-abc", "turn": 7}

    def test_no_checkpoint_returns_empty(self):
        with _mock_checkpointer(None):
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
        tup = MagicMock()
        tup.checkpoint = {"channel_values": {}}  # no active_task_id at all
        tup.config = {"configurable": {"thread_id": "sess-xyz"}}
        with _mock_checkpointer(tup):
            result = get_current_session()
        assert result["session_id"] == "sess-xyz"

    def test_exception_returns_empty(self):
        graph = MagicMock()
        graph.checkpointer = MagicMock()
        graph.checkpointer.list.side_effect = RuntimeError("boom")
        with patch("langchain_learning.session_graph._graph", graph):
            result = get_current_session()
        assert result == {}
