"""Test LangGraph fan-out/fan-in state merge with SqliteSaver.

Verifies that parallel nodes writing different state keys are correctly merged,
and that SqliteSaver checkpoint integrity is maintained after a fan-in.
"""
import sqlite3
import tempfile
from typing import TypedDict

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END


# ---------------------------------------------------------------------------
# Minimal state for the test
# ---------------------------------------------------------------------------

class FanState(TypedDict, total=False):
    input: str
    result_a: str
    result_b: str
    result_c: str
    merged: str


# ---------------------------------------------------------------------------
# Dummy nodes
# ---------------------------------------------------------------------------

def node_a(state: FanState) -> dict:
    return {"result_a": f"A:{state['input']}"}


def node_b(state: FanState) -> dict:
    return {"result_b": f"B:{state['input']}"}


def node_c(state: FanState) -> dict:
    return {"result_c": f"C:{state['input']}"}


def merge_node(state: FanState) -> dict:
    return {"merged": f"{state['result_a']}|{state['result_b']}|{state['result_c']}"}


def _build_fanout_graph(checkpointer=None):
    builder = StateGraph(FanState)
    builder.add_node("node_a", node_a)
    builder.add_node("node_b", node_b)
    builder.add_node("node_c", node_c)
    builder.add_node("merge_node", merge_node)

    # Fan-out: START → a, b, c in parallel
    builder.add_edge(START, "node_a")
    builder.add_edge(START, "node_b")
    builder.add_edge(START, "node_c")

    # Fan-in: a, b, c → merge
    builder.add_edge("node_a", "merge_node")
    builder.add_edge("node_b", "merge_node")
    builder.add_edge("node_c", "merge_node")

    builder.add_edge("merge_node", END)
    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFanOutMergeNoCheckpointer:
    """Basic fan-out/fan-in correctness without persistence."""

    def test_all_parallel_keys_present(self):
        graph = _build_fanout_graph()
        result = graph.invoke({"input": "hello"})
        assert result["result_a"] == "A:hello"
        assert result["result_b"] == "B:hello"
        assert result["result_c"] == "C:hello"

    def test_merge_node_sees_all_outputs(self):
        graph = _build_fanout_graph()
        result = graph.invoke({"input": "test"})
        assert result["merged"] == "A:test|B:test|C:test"

    def test_parallel_nodes_dont_clobber_each_other(self):
        graph = _build_fanout_graph()
        result = graph.invoke({"input": "x"})
        # All three keys must survive — no key should be missing or overwritten
        assert "result_a" in result
        assert "result_b" in result
        assert "result_c" in result


class TestFanOutMergeWithSqliteSaver:
    """Fan-out/fan-in with SqliteSaver — checkpoint integrity across invocations."""

    @pytest.fixture
    def checkpointer(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as f:
            conn = sqlite3.connect(f.name, check_same_thread=False)
            yield SqliteSaver(conn)
            conn.close()

    def test_checkpoint_written_after_fanin(self, checkpointer):
        graph = _build_fanout_graph(checkpointer)
        cfg = {"configurable": {"thread_id": "sess-1"}}
        result = graph.invoke({"input": "ping"}, config=cfg)
        assert result["merged"] == "A:ping|B:ping|C:ping"

        # Checkpoint must exist and contain merged state
        saved = graph.get_state(cfg)
        assert saved is not None
        assert saved.values["merged"] == "A:ping|B:ping|C:ping"

    def test_second_invocation_resumes_from_checkpoint(self, checkpointer):
        graph = _build_fanout_graph(checkpointer)
        cfg = {"configurable": {"thread_id": "sess-2"}}

        graph.invoke({"input": "first"}, config=cfg)
        result2 = graph.invoke({"input": "second"}, config=cfg)

        # Second invoke should have fresh parallel results
        assert result2["result_a"] == "A:second"
        assert result2["result_b"] == "B:second"
        assert result2["merged"] == "A:second|B:second|C:second"

    def test_different_sessions_are_isolated(self, checkpointer):
        graph = _build_fanout_graph(checkpointer)
        graph.invoke({"input": "alpha"}, config={"configurable": {"thread_id": "sess-A"}})
        graph.invoke({"input": "beta"},  config={"configurable": {"thread_id": "sess-B"}})

        state_a = graph.get_state({"configurable": {"thread_id": "sess-A"}})
        state_b = graph.get_state({"configurable": {"thread_id": "sess-B"}})

        assert state_a.values["merged"] == "A:alpha|B:alpha|C:alpha"
        assert state_b.values["merged"] == "A:beta|B:beta|C:beta"
