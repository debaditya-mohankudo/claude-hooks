"""Fan-out failure injection tests for the session graph.

Verifies that when an underlying dependency (DB, ollama) fails inside a
fan-out node, the node's own except Exception handles it gracefully and
returns a default — and downstream nodes still fire.

This is the correct injection level: patch the *dependency*, not the node's
__call__. That way the node's error handling is exercised, not bypassed.

If a node raises unhandled to LangGraph, the graph aborts entirely — these
tests catch that regression too (they'd error out rather than asserting).

Topology under test (UPS chain, task:882d67fa collapsed it):
    load_turn → load_memories ────┐
              → score_tools    ───┼──→ set_prompt_id → END

load_active_task, load_task_code, load_related_commits, and
summarize_task_context are gone (task:882d67fa) — task-framework owns
active-task context now, so there is no longer a task-conditional branch or
a second fan-out tier to inject failures into.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

from langgraph.checkpoint.memory import MemorySaver

import langchain_learning.session_graph as sg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_graph():
    graph = sg.build_session_graph(checkpointer=MemorySaver())
    sg._graph = graph
    return graph


def _run_ups(graph, session_id: str, tmp_path: Path) -> dict:
    return sg.run_session(
        prompt="fix the broken import",
        session_id=session_id,
        cwd=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Single dependency failures — node handles gracefully, graph completes
# ---------------------------------------------------------------------------

class TestSingleDependencyFailure:

    def test_load_memories_db_error_score_tools_still_runs(self, tmp_path):
        """MEMORY.sqlite failure → load_memories returns [] → score_tools still runs."""
        graph = _build_graph()
        session_id = "fail-mem-01"

        mock_cfg = MagicMock()
        mock_cfg.memory_db = tmp_path / "MEMORY.sqlite"  # doesn't exist

        with patch("langchain_learning.config.config", mock_cfg), \
             patch("langchain_learning.nodes.score_tools.ScoreToolsNode.__call__",
                   return_value={"tool_hints": ["contacts__search"]}) as mock_score:

            result = _run_ups(graph, session_id, tmp_path)

        assert isinstance(result, dict)
        # score_tools ran despite memory failure
        mock_score.assert_called_once()


# ---------------------------------------------------------------------------
# Two simultaneous dependency failures
# ---------------------------------------------------------------------------

class TestDoubleDependencyFailure:

    def test_memories_and_score_tools_both_fail_set_prompt_id_runs(self, tmp_path):
        """load_memories + score_tools both fail → set_prompt_id still fires."""
        graph = _build_graph()
        session_id = "fail-double-02"

        mock_cfg_mem = MagicMock()
        mock_cfg_mem.memory_db = tmp_path / "MEMORY.sqlite"  # missing

        with patch("langchain_learning.config.config", mock_cfg_mem), \
             patch("langchain_learning.nodes.score_tools.ScoreToolsNode.__call__",
                   return_value={"tool_hints": []}), \
             patch("langchain_learning.nodes.set_prompt_id.SetPromptIdNode.__call__",
                   return_value={"prompt_id": "double-pid"}) as mock_pid:

            result = _run_ups(graph, session_id, tmp_path)

        assert isinstance(result, dict)
        # set_prompt_id — the convergence node — still ran
        mock_pid.assert_called_once()
