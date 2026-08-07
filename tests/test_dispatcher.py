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

from dispatcher import (
    _extract_prompt,
    _get_claude_session_id,
    _format_system_prompt,
    _enforce_context_budget,
    _CONTEXT_TOKEN_BUDGET,
    _TASK_BODY_CHAR_CAP,
    _read_last_usage,
    _maybe_context_size_nudge,
    _CONTEXT_NUDGE_SHOWN_BAND,
    _CONTEXT_NUDGE_THRESHOLD,
    _CONTEXT_NUDGE_STEP,
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
    base = {"session_id": "", "prompt_id": "", "memories": [],
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


def test_includes_memories():
    mem = {"name": "my-mem", "body": "remember this"}
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


# ── _read_last_usage / _maybe_context_size_nudge (task:e849c7ad) ─────────────

import json


def _write_transcript(tmp_path, lines):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return str(path)


def test_read_last_usage_missing_file_returns_none(tmp_path):
    assert _read_last_usage(str(tmp_path / "nope.jsonl")) is None


def test_read_last_usage_empty_path_returns_none():
    assert _read_last_usage("") is None


def test_read_last_usage_sums_usage_fields(tmp_path):
    path = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 900,
        }}},
    ])
    assert _read_last_usage(path) == 1002


def test_read_last_usage_takes_most_recent_usage(tmp_path):
    path = _write_transcript(tmp_path, [
        {"message": {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 100}}},
        {"message": {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 500}}},
    ])
    assert _read_last_usage(path) == 501


def test_read_last_usage_malformed_last_line_degrades_to_none(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text("not json\n")
    assert _read_last_usage(str(path)) is None


def test_read_last_usage_skips_lines_without_usage(tmp_path):
    path = _write_transcript(tmp_path, [
        {"message": {"usage": {"input_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 200}}},
        {"type": "attachment", "message": {}},
    ])
    assert _read_last_usage(path) == 205


def test_context_size_nudge_below_threshold_returns_none(tmp_path):
    path = _write_transcript(tmp_path, [
        {"message": {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 500}}},
    ])
    _CONTEXT_NUDGE_SHOWN_BAND.clear()
    result = _maybe_context_size_nudge({"transcript_path": path}, "sess-below")
    assert result is None


def test_context_size_nudge_fires_once_per_band(tmp_path):
    _CONTEXT_NUDGE_SHOWN_BAND.clear()
    session_id = "sess-band"
    total = _CONTEXT_NUDGE_THRESHOLD + 1
    path = _write_transcript(tmp_path, [
        {"message": {"usage": {"input_tokens": total, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
    ])
    first = _maybe_context_size_nudge({"transcript_path": path}, session_id)
    assert first is not None
    assert first["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "additionalContext" in first["hookSpecificOutput"]

    second = _maybe_context_size_nudge({"transcript_path": path}, session_id)
    assert second is None


def test_context_size_nudge_fires_again_on_next_band(tmp_path):
    _CONTEXT_NUDGE_SHOWN_BAND.clear()
    session_id = "sess-next-band"
    path_a = _write_transcript(tmp_path, [
        {"message": {"usage": {"input_tokens": _CONTEXT_NUDGE_THRESHOLD + 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
    ])
    assert _maybe_context_size_nudge({"transcript_path": path_a}, session_id) is not None

    # Overwrite the same file with usage from the next band.
    higher = _CONTEXT_NUDGE_THRESHOLD + _CONTEXT_NUDGE_STEP + 1
    path_a_obj = tmp_path / "transcript.jsonl"
    path_a_obj.write_text(json.dumps({"message": {"usage": {
        "input_tokens": higher, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }}}) + "\n")
    assert _maybe_context_size_nudge({"transcript_path": str(path_a_obj)}, session_id) is not None


def test_context_size_nudge_no_session_id_returns_none(tmp_path):
    path = _write_transcript(tmp_path, [
        {"message": {"usage": {"input_tokens": _CONTEXT_NUDGE_THRESHOLD + 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
    ])
    assert _maybe_context_size_nudge({"transcript_path": path}, "") is None
