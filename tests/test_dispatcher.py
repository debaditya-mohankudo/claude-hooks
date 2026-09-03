"""Tests for hooks/dispatcher.py — pure functions only.

The _handle_* functions invoke LangGraph session graphs and are integration-level.
Tests here cover the pure extractors and validators that can be tested in isolation.
This is intentional: difficulty adding unit tests to the handlers is a known signal
that they carry too much orchestration logic (monolith smell — noted for future refactor).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

# Ensure hooks/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

from dispatcher import (
    _extract_prompt,
    _get_claude_session_id,
    _format_system_prompt,
    _read_last_usage,
    _maybe_context_size_nudge,
    _CONTEXT_NUDGE_SHOWN_BAND,
    _CONTEXT_NUDGE_THRESHOLD,
    _CONTEXT_NUDGE_STEP,
    _handle_user_prompt_submit,
    _handle_session_end,
    _load_vault_context,
)
import dispatcher as _dispatcher


# ── _load_vault_context — EDEADLK fallback to the persisted cache ─────────────

from unittest.mock import MagicMock


def _fake_file(exc: Exception) -> MagicMock:
    m = MagicMock()
    m.read_text.side_effect = exc
    return m


def test_load_vault_context_falls_back_to_cache_on_read_error(monkeypatch):
    # read raises EDEADLK (iCloud lock on a dataless file); the cache already
    # holds a good copy from a previous successful read. Use a plain dict so
    # the test never touches the real persisted cache file.
    monkeypatch.setattr(_dispatcher, "_LIFE_OS_FILES",
                        {"dev_personality": _fake_file(OSError(11, "Resource deadlock avoided"))})
    monkeypatch.setattr(_dispatcher, "_vault_context_cache", {"dev_personality": "cached identity"})

    assert _load_vault_context() == {"dev_personality": "cached identity"}


def test_load_vault_context_drops_key_when_no_cache(monkeypatch):
    monkeypatch.setattr(_dispatcher, "_LIFE_OS_FILES",
                        {"dev_personality": _fake_file(OSError(11, "Resource deadlock avoided"))})
    monkeypatch.setattr(_dispatcher, "_vault_context_cache", {})

    assert _load_vault_context() == {}


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
    base = {"session_id": "", "prompt_id": "", "memories": [], "tool_hints": []}
    base.update(kwargs)
    return base


def test_empty_ctx_returns_empty_string():
    assert _format_system_prompt(_base_ctx()) == ""


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


def test_includes_dev_personality():
    result = _format_system_prompt(_base_ctx(
        vault_context={"dev_personality": "compounding reward"},
    ))
    assert "## Dev personality" in result
    assert "compounding reward" in result


def test_omits_dev_personality_block_when_absent():
    result = _format_system_prompt(_base_ctx(vault_context={}))
    assert "## Dev personality" not in result


def test_work_context_no_longer_rendered():
    # work.md was replaced by dev_personality.md (task:9bbd67dd) — even if a
    # stale "work" key is still present in vault_context, it must not render.
    result = _format_system_prompt(_base_ctx(vault_context={"work": "terse responses"}))
    assert "## Work context" not in result
    assert "terse responses" not in result


# Execution contract, task decisions/memories/history, relevant code, and
# related tasks/commits rendering tests removed (task:882d67fa) — that context
# is task-framework's now; _format_system_prompt does not render any of it.
#
# Active task came back (task:c2e36050) via a render of whatever task-framework
# last pushed to hooks/session_state.get_active_task (task:996cc8f0), then was
# removed again (task:8be768df). A taskfw PostToolUse-driven drift nudge briefly
# filled that role and was itself removed (task:00d9483f). The push cache that
# fed ctx["active_task"] is now gone too (task:173e6846) — this handler no
# longer populates it and _format_system_prompt has no active-task branch.
# The guarantee that matters outlives all of that: the assembled prompt never
# carries an active-task block, no matter what ctx holds.

def test_never_renders_active_task_block():
    result = _format_system_prompt(_base_ctx(
        session_id="sess01", prompt_id="ppp1",
        memories=[{"name": "m", "domain": "d", "body": "b"}],
        tool_hints=[{"tool_name": "t", "skill": "s", "count": 1}],
        vault_context={"dev_personality": "x"},
    ))
    assert "## Active task" not in result

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


# ── /clear session-end brief close ────────────────────────────────────────────
#
# /clear ends the old session_id via SessionEnd and starts a brand new one via
# SessionStart — it does not reuse the session_id, so SessionBrief.close()
# fires from _handle_session_end with the ending session's own session_id,
# not from a text match on the UserPromptSubmit prompt (task:3d643f7c).

def test_extract_prompt_preserves_hyphenated_command_name_tag():
    """[a-z_]+ tag-stripping must not eat hyphenated tags like <command-name>."""
    raw = "<local-command-caveat>noise</local-command-caveat>\n<command-name>/clear</command-name>"
    assert "<command-name>/clear</command-name>" in _extract_prompt({"prompt": raw})


