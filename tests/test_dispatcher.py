"""Tests for hooks/dispatcher.py — pure functions only.

The _handle_* functions invoke LangGraph session graphs and are integration-level.
Tests here cover the pure extractors and validators that can be tested in isolation.
This is intentional: difficulty adding unit tests to the handlers is a known signal
that they carry too much orchestration logic (monolith smell — noted for future refactor).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure hooks/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

from unittest.mock import MagicMock, patch

from dispatcher import (
    _extract_prompt,
    _get_claude_session_id,
    _format_system_prompt,
    _enforce_context_budget,
    _CONTEXT_TOKEN_BUDGET,
    _TASK_BODY_CHAR_CAP,
    _maybe_drift_reflection_nudge,
    _DRIFT_EDIT_COUNTS,
    _DRIFT_REFLECTION_INTERVAL,
)


# ── _get_claude_session_id ────────────────────────────────────────────────────

def test_extracts_session_id():
    assert _get_claude_session_id({"session_id": "abc123"}) == "abc123"


def test_missing_session_id_returns_empty():
    assert _get_claude_session_id({}) == ""


# ── _extract_prompt ───────────────────────────────────────────────────────────

def test_extracts_top_level_prompt():
    assert _extract_prompt({"prompt": "hello"}) == "hello"


def test_extracts_prompt_from_message_string():
    result = _extract_prompt({"message": {"content": "hello from message"}})
    assert result == "hello from message"


def test_extracts_prompt_from_message_blocks():
    result = _extract_prompt({"message": {"content": [
        {"type": "text", "text": "block one "},
        {"type": "text", "text": "block two"},
    ]}})
    assert result == "block one block two"


def test_strips_xml_context_tags():
    result = _extract_prompt({"prompt": "<system_reminder>noise</system_reminder>\nreal prompt"})
    assert "noise" not in result
    assert "real prompt" in result


def test_returns_empty_when_no_prompt():
    assert _extract_prompt({}) == ""


# ── _format_system_prompt ─────────────────────────────────────────────────────

def _base_ctx(**kwargs) -> dict:
    base = {"session_id": "", "prompt_id": "", "domains": [], "memories": [],
            "tool_hints": [], "active_task_id": "", "active_task_title": "",
            "task_body": "", "execution_contract": "", "mid_task_decisions": [],
            "task_memories": [], "task_context": [], "task_rag_chunks": [], "related_tasks": []}
    base.update(kwargs)
    return base


def test_empty_ctx_returns_empty_string():
    assert _format_system_prompt(_base_ctx()) == ""


# ── _enforce_context_budget ───────────────────────────────────────────────────

def test_under_budget_leaves_memories_untouched():
    ctx = _base_ctx(memories=[{"name": "m1", "body": "short body"}])
    _enforce_context_budget(ctx)
    assert len(ctx["memories"]) == 1


def test_over_budget_drops_lowest_scored_memories_from_tail():
    # Pre-sorted descending by score: highest-value memory first, lowest last.
    # A single ~5-word body is well under budget; padding one entry huge forces a trim.
    huge_body = "word " * 20000  # far exceeds _CONTEXT_TOKEN_BUDGET on its own
    ctx = _base_ctx(memories=[
        {"name": "high-value", "body": "short"},
        {"name": "low-value", "body": huge_body},
    ])
    _enforce_context_budget(ctx)
    remaining = [m["name"] for m in ctx["memories"]]
    assert "high-value" in remaining
    assert "low-value" not in remaining


def test_drops_until_empty_if_still_over_budget():
    huge_body = "word " * 20000
    ctx = _base_ctx(memories=[
        {"name": "a", "body": huge_body},
        {"name": "b", "body": huge_body},
    ])
    _enforce_context_budget(ctx)
    assert ctx["memories"] == []


def test_related_tasks_and_commits_untouched_even_when_over_budget():
    huge_body = "word " * 20000
    ctx = _base_ctx(
        memories=[{"name": "a", "body": huge_body}],
        related_tasks=[{"id": "t1", "title": "x", "body_snippet": "snippet"}],
    )
    ctx["related_commits"] = [{"commit_hash": "abc123", "file": "f.py", "snippet": "diff"}]
    _enforce_context_budget(ctx)
    assert ctx["related_tasks"] == [{"id": "t1", "title": "x", "body_snippet": "snippet"}]
    assert ctx["related_commits"] == [{"commit_hash": "abc123", "file": "f.py", "snippet": "diff"}]


def test_includes_turn_state_block():
    result = _format_system_prompt(_base_ctx(session_id="sess01", prompt_id="ppp1"))
    assert "## Turn state" in result
    assert "sess01" in result
    assert "ppp1" in result


def test_includes_active_domains():
    result = _format_system_prompt(_base_ctx(domains=["market-intel"]))
    assert "market-intel" in result


def test_includes_memories():
    mem = {"name": "my-mem", "domain": "global", "body": "remember this"}
    result = _format_system_prompt(_base_ctx(memories=[mem]))
    assert "## Injected memories" in result
    assert "remember this" in result


def test_includes_tool_hints():
    hint = {"tool_name": "tasks__create", "skill": "task-framework", "count": 5}
    result = _format_system_prompt(_base_ctx(tool_hints=[hint]))
    assert "## Suggested tools" in result
    assert "tasks__create" in result


def test_truncates_oversized_task_body():
    huge_body = "x" * (_TASK_BODY_CHAR_CAP + 500)
    result = _format_system_prompt(_base_ctx(
        active_task_id="t1", active_task_title="Big epic", task_body=huge_body,
    ))
    assert "...[truncated]" in result
    assert len(result) < len(huge_body) + 200


def test_leaves_small_task_body_untouched():
    result = _format_system_prompt(_base_ctx(
        active_task_id="t1", active_task_title="Small task", task_body="short body",
    ))
    assert "short body" in result


# ── _maybe_drift_reflection_nudge ─────────────────────────────────────────────

def _mock_checkpoint(values: dict):
    state = MagicMock()
    state.values = values
    graph = MagicMock()
    graph.get_state.return_value = state
    return patch("langchain_learning.session_graph.get_session_graph", return_value=graph)


def test_no_nudge_for_non_edit_tools():
    assert _maybe_drift_reflection_nudge("Bash", {"file_path": "x.py"}, "sess1") is None


def test_no_nudge_without_active_task():
    with _mock_checkpoint({}):
        assert _maybe_drift_reflection_nudge("Edit", {"file_path": "x.py"}, "sess1") is None


def test_no_nudge_before_interval_reached():
    _DRIFT_EDIT_COUNTS.clear()
    with _mock_checkpoint({"active_task_id": "t1", "active_task_title": "Some task"}):
        for _ in range(_DRIFT_REFLECTION_INTERVAL - 1):
            result = _maybe_drift_reflection_nudge("Edit", {"file_path": "src/foo.py"}, "sess1")
    assert result is None


def test_nudges_once_interval_reached():
    _DRIFT_EDIT_COUNTS.clear()
    with _mock_checkpoint({"active_task_id": "t1", "active_task_title": "Some task"}):
        result = None
        for _ in range(_DRIFT_REFLECTION_INTERVAL):
            result = _maybe_drift_reflection_nudge("Edit", {"file_path": "src/foo.py"}, "sess1")
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "task:t1" in result["hookSpecificOutput"]["additionalContext"]


def test_nudge_recurs_every_interval():
    _DRIFT_EDIT_COUNTS.clear()
    with _mock_checkpoint({"active_task_id": "t1", "active_task_title": "Some task"}):
        results = [
            _maybe_drift_reflection_nudge("Edit", {"file_path": "src/foo.py"}, "sess1")
            for _ in range(_DRIFT_REFLECTION_INTERVAL * 2)
        ]
    fired = [i for i, r in enumerate(results, start=1) if r is not None]
    assert fired == [_DRIFT_REFLECTION_INTERVAL, _DRIFT_REFLECTION_INTERVAL * 2]


def test_counts_are_scoped_per_session_and_task():
    _DRIFT_EDIT_COUNTS.clear()
    with _mock_checkpoint({"active_task_id": "t1", "active_task_title": "Some task"}):
        for _ in range(_DRIFT_REFLECTION_INTERVAL - 1):
            _maybe_drift_reflection_nudge("Edit", {"file_path": "src/foo.py"}, "sess1")
        # a different task should not inherit sess1/t1's near-complete count
        result = _maybe_drift_reflection_nudge("Edit", {"file_path": "src/foo.py"}, "sess2")
    assert result is None


def test_includes_active_task():
    result = _format_system_prompt(_base_ctx(
        active_task_id="abc123", active_task_title="Fix the bug", task_body="details"
    ))
    assert "## Active task" in result
    assert "abc123" in result
    assert "Fix the bug" in result


def test_includes_mid_task_decisions():
    result = _format_system_prompt(_base_ctx(mid_task_decisions=["use postgres"]))
    assert "## Task decisions" in result
    assert "use postgres" in result


# ── execution_contract rendering ──────────────────────────────────────────────

def test_includes_execution_contract():
    result = _format_system_prompt(_base_ctx(
        active_task_id="abc123", active_task_title="Fix the bug",
        execution_contract="You are executing task:abc123 — Fix the bug.\nFinish decisively.",
    ))
    assert "### Execution contract" in result
    assert "Finish decisively" in result


def test_omits_execution_contract_section_when_absent():
    result = _format_system_prompt(_base_ctx(
        active_task_id="abc123", active_task_title="Fix the bug",
    ))
    assert "### Execution contract" not in result


def test_execution_contract_not_truncated_even_when_huge():
    # Unlike task_body, the contract is a fixed template with no upstream cap —
    # this proves it isn't accidentally routed through _TASK_BODY_CHAR_CAP.
    huge_contract = "x" * (_TASK_BODY_CHAR_CAP + 500)
    result = _format_system_prompt(_base_ctx(
        active_task_id="t1", active_task_title="Big task", execution_contract=huge_contract,
    ))
    assert huge_contract in result
    assert "...[truncated]" not in result


def test_execution_contract_untouched_by_context_budget_enforcement():
    # _enforce_context_budget only trims ctx["memories"] — confirm the contract
    # isn't part of that eviction path regardless of how large memories get.
    huge_body = "word " * 20000
    contract = "You are executing task:t1 — Big task."
    ctx = _base_ctx(
        active_task_id="t1", active_task_title="Big task", execution_contract=contract,
        memories=[{"name": "a", "body": huge_body}],
    )
    _enforce_context_budget(ctx)
    assert ctx["execution_contract"] == contract


def test_includes_related_tasks():
    result = _format_system_prompt(_base_ctx(
        related_tasks=[{"id": "t1", "title": "Prior task", "body_snippet": ""}]
    ))
    assert "## Related past tasks" in result
    assert "Prior task" in result


def test_includes_task_history_single_session():
    ctx = [{"session_id": "sess01", "turn": 3, "summary": "did stuff", "tools": "Bash"}]
    result = _format_system_prompt(_base_ctx(task_context=ctx))
    assert "## Task history" in result
    assert "turn 3" in result
    assert "did stuff" in result


def test_task_history_multi_session_shows_session_id():
    ctx = [
        {"session_id": "aaa", "turn": 1, "summary": "s1", "tools": ""},
        {"session_id": "bbb", "turn": 2, "summary": "s2", "tools": ""},
    ]
    result = _format_system_prompt(_base_ctx(task_context=ctx))
    assert "[aaa]" in result
    assert "[bbb]" in result


def test_includes_rag_chunks():
    chunk = {"name": "MyClass", "module": "hooks.gates", "file": "hooks/gates.py", "line": 42}
    result = _format_system_prompt(_base_ctx(task_rag_chunks=[chunk]))
    assert "## Relevant code" in result
    assert "MyClass" in result

# _check_task_body_format and its tests were removed here (task:87ec7876). The
# tool it gated, mcp__claude-hooks__tasks__create, has no implementation left
# to call — handle_create_scaffolded was in src/tools/tasks.py, deleted in the
# same task.
