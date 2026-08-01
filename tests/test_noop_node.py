"""Tests for NoopNode — sound-alert one-shot + per-thread checkpoint trim (task:b3964f85)."""
from __future__ import annotations

import pytest

from langchain_learning.nodes.noop import NoopNode, _CHECKPOINT_ROW_CAP, _trim_thread_checkpoints


def _state(**kwargs) -> dict:
    base = {"event_type": "stop", "session_id": "sess0001", "stop_alert_sent": False}
    base.update(kwargs)
    return base


class TestSoundAlert:
    def test_unknown_event_logs_and_returns_empty(self):
        result = NoopNode()({"event_type": "bogus", "session_id": "sess0001"})
        assert result == {}

    def test_first_stop_of_turn_marks_alert_sent(self):
        result = NoopNode()(_state())
        assert result == {"stop_alert_sent": True}

    def test_second_stop_of_turn_is_silent(self):
        result = NoopNode()(_state(stop_alert_sent=True))
        assert result == {}

    def test_missing_session_id_still_fires_alert(self):
        # No session_id -> _trim_thread_checkpoints is skipped, but the sound
        # alert itself must not depend on it.
        result = NoopNode()(_state(session_id=""))
        assert result["stop_alert_sent"] is True


@pytest.fixture()
def mem_graph():
    """A live MemorySaver-backed graph — mirrors test_langchain_session_graph.py's
    mem_graph fixture, needed here to give _trim_thread_checkpoints a real
    checkpointer.storage to operate on."""
    from langgraph.checkpoint.memory import MemorySaver
    import langchain_learning.session_graph as sg
    prev = sg._graph
    sg._graph = sg.build_session_graph(checkpointer=MemorySaver())
    yield sg
    sg._graph = prev


class TestCheckpointTrim:
    def test_noop_without_live_graph(self):
        """No live graph set (sg._graph is None) — must not raise."""
        import langchain_learning.session_graph as sg
        prev = sg._graph
        sg._graph = None
        try:
            _trim_thread_checkpoints("some-thread")
        finally:
            sg._graph = prev

    def test_noop_for_thread_with_no_history(self, mem_graph):
        _trim_thread_checkpoints("never-seen-thread")  # must not raise

    def test_under_cap_is_untouched(self, mem_graph):
        sg = mem_graph
        for i in range(5):
            sg.run_session(f"prompt {i}", session_id="thread-a", cwd="/tmp")

        checkpointer = sg._graph.checkpointer
        before = sum(len(ns) for ns in checkpointer.storage["thread-a"].values())
        _trim_thread_checkpoints("thread-a", row_cap=_CHECKPOINT_ROW_CAP)
        after = sum(len(ns) for ns in checkpointer.storage["thread-a"].values())
        assert before == after

    def test_over_cap_evicts_oldest(self, mem_graph):
        sg = mem_graph
        for i in range(20):
            sg.run_session(f"prompt {i}", session_id="thread-b", cwd="/tmp")

        checkpointer = sg._graph.checkpointer
        total_before = sum(len(ns) for ns in checkpointer.storage["thread-b"].values())
        assert total_before > 5  # sanity: this thread actually has history to trim

        _trim_thread_checkpoints("thread-b", row_cap=5)

        for ns, ns_checkpoints in checkpointer.storage["thread-b"].items():
            assert len(ns_checkpoints) <= 5

    def test_evicted_writes_are_cleaned_up(self, mem_graph):
        """Evicting a checkpoint must also drop its (thread_id, ns, checkpoint_id)
        entry from checkpointer.writes, mirroring the old SqliteSaver-era
        orphaned-writes cleanup — otherwise writes accumulates unboundedly even
        though storage itself is capped."""
        sg = mem_graph
        for i in range(20):
            sg.run_session(f"prompt {i}", session_id="thread-c", cwd="/tmp")

        checkpointer = sg._graph.checkpointer
        _trim_thread_checkpoints("thread-c", row_cap=5)

        surviving_ids = {
            cid for ns_checkpoints in checkpointer.storage["thread-c"].values()
            for cid in ns_checkpoints
        }
        for (tid, _ns, cid) in list(checkpointer.writes.keys()):
            if tid == "thread-c":
                assert cid in surviving_ids

    def test_does_not_affect_other_threads(self, mem_graph):
        sg = mem_graph
        for i in range(20):
            sg.run_session(f"prompt {i}", session_id="thread-d", cwd="/tmp")
        for i in range(3):
            sg.run_session(f"prompt {i}", session_id="thread-e", cwd="/tmp")

        checkpointer = sg._graph.checkpointer
        before_e = sum(len(ns) for ns in checkpointer.storage["thread-e"].values())
        _trim_thread_checkpoints("thread-d", row_cap=5)
        after_e = sum(len(ns) for ns in checkpointer.storage["thread-e"].values())
        assert before_e == after_e

    def test_noop_node_call_trims_via_stop_event(self, mem_graph):
        """End-to-end: NoopNode itself (not the helper directly) trims on a
        real Stop event when routed through the graph, not just via
        run_session's UserPromptSubmit path."""
        sg = mem_graph
        for i in range(20):
            sg.run_session(f"prompt {i}", session_id="thread-f", cwd="/tmp")

        checkpointer = sg._graph.checkpointer
        total_before = sum(len(ns) for ns in checkpointer.storage["thread-f"].values())
        assert total_before > 5

        NoopNode()({"event_type": "stop", "session_id": "thread-f", "stop_alert_sent": True})

        total_after = sum(len(ns) for ns in checkpointer.storage["thread-f"].values())
        assert total_after <= _CHECKPOINT_ROW_CAP
