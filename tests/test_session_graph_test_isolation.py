"""Unit tests for get_session_graph's test/production routing (task:b63088a1).

Integration tests used to post session_ids straight into the shared production
MemorySaver, which (since it never evicts) left permanent stale threads behind —
the direct root cause of task:a4531510's cross-thread staleness bug. These tests
cover the routing logic itself in isolation, without needing a live server.
"""
from unittest.mock import patch

import langchain_learning.session_graph as sg


def _reset_graphs():
    sg._graph = None
    sg._test_graph = None


def test_non_prefixed_session_id_uses_production_graph():
    _reset_graphs()
    graph = sg.get_session_graph("real-session-abc")
    assert graph is sg._graph
    assert sg._test_graph is None  # never touched


def test_empty_session_id_uses_production_graph():
    _reset_graphs()
    graph = sg.get_session_graph("")
    assert graph is sg._graph
    assert sg._test_graph is None


def test_prefixed_session_id_uses_isolated_test_graph():
    _reset_graphs()
    graph = sg.get_session_graph(f"{sg.TEST_SESSION_PREFIX}my-test-session")
    assert graph is sg._test_graph
    assert sg._graph is None  # production graph never lazily built by this call


def test_production_and_test_graphs_are_distinct_objects():
    _reset_graphs()
    prod = sg.get_session_graph("real-session")
    test = sg.get_session_graph(f"{sg.TEST_SESSION_PREFIX}test-session")
    assert prod is not test
    assert prod.checkpointer is not test.checkpointer


def test_repeated_calls_reuse_the_same_graph_instance():
    _reset_graphs()
    first = sg.get_session_graph(f"{sg.TEST_SESSION_PREFIX}a")
    second = sg.get_session_graph(f"{sg.TEST_SESSION_PREFIX}b")
    # Different session_ids, same prefix -> same underlying test graph/checkpointer,
    # just different threads within it (mirrors production behavior: many real
    # sessions share one _graph, distinguished only by thread_id).
    assert first is second


def test_checkpoint_written_via_test_graph_is_invisible_to_production_graph():
    """The actual isolation guarantee: a checkpoint written under a
    test-prefixed session_id must not be readable through the production
    graph, even by explicit thread_id — they are genuinely separate stores,
    not just a naming convention on a shared one."""
    _reset_graphs()
    test_sid = f"{sg.TEST_SESSION_PREFIX}isolated-session"
    cfg = {"configurable": {"thread_id": test_sid}}

    test_graph = sg.get_session_graph(test_sid)
    test_graph.update_state(cfg, {"turn": 5})

    # Force production graph to exist too, then check it directly.
    prod_graph = sg.get_session_graph("unrelated-real-session")
    prod_state = prod_graph.get_state(cfg)
    assert prod_state is None or not prod_state.values


def test_run_session_routes_test_prefixed_session_to_isolated_graph(tmp_path):
    _reset_graphs()
    with patch("langchain_learning.config.config") as cfg, \
         patch("src.config.config") as src_cfg:
        cfg.memory_db = tmp_path / "MEMORY.sqlite"
        src_cfg.memory_db = tmp_path / "MEMORY.sqlite"
        test_sid = f"{sg.TEST_SESSION_PREFIX}run-session-test"
        sg.run_session(prompt="hello", session_id=test_sid, cwd="/tmp")
    assert sg._test_graph is not None
    # Confirm the checkpoint actually landed in the test graph, not production.
    state = sg._test_graph.get_state({"configurable": {"thread_id": test_sid}})
    assert state is not None and state.values.get("session_id") == test_sid
