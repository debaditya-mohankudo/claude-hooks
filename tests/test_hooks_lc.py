"""Tests for the hook entry point (dispatcher.main) end to end.

Session state is hooks/session_store.py (task:31fb711d); tests reset it.

Covered:
  - pre_tool_use_lc:      Gate deny/allow, memory-tool skip, fail-open
  - tool_usage_logger_lc: tool_hints upsert, session recording, skip conditions
  - stop_hook_lc:         stopword filtering, state transition, empty-session skip
"""
import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "hooks"))

from hooks import session_store
from src.db.schema import MCP_TOOL_HINTS_DDL


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_session_store():
    session_store.reset()
    yield
    session_store.reset()


@pytest.fixture()
def tool_hints_db(tmp_path):
    path = tmp_path / "tool_hints.sqlite"
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(MCP_TOOL_HINTS_DDL)
        conn.commit()
    return path


# ---------------------------------------------------------------------------
# pre_tool_use_lc
# ---------------------------------------------------------------------------

class TestPreToolUseLc:
    """Gate enforcement."""

    def _run(self, hook_input: dict, checkpoints_db_path: Path | None = None, tmp_path: Path | None = None) -> dict:
        import hooks.dispatcher as hook_mod

        with patch("sys.argv", ["dispatcher.py", "PreToolUse"]), \
             patch("sys.stdin", StringIO(json.dumps(hook_input))), \
             patch("sys.stdout", new_callable=StringIO) as mock_out:
            hook_mod.main()
            out = mock_out.getvalue().strip()

        return json.loads(out) if out else {}

    def test_non_mcp_tool_passes_through(self, tmp_path):
        result = self._run(
            {"tool_name": "Bash", "session_id": "s1", "tool_use_id": "p1"},
            tmp_path=tmp_path,
        )
        assert result == {}

    def test_memory_tool_skipped(self, tmp_path):
        result = self._run(
            {"tool_name": "mcp__local-mac__memory__add", "session_id": "s1", "tool_use_id": "p1"},
            tmp_path=tmp_path,
        )
        assert result == {}

    def test_ungated_mcp_tool_allowed(self, tmp_path):
        result = self._run(
            {"tool_name": "mcp__local-mac__music__play", "session_id": "s3", "tool_use_id": "p3"},
            tmp_path=tmp_path,
        )
        assert "hookSpecificOutput" not in result

    def test_fail_open_on_missing_session_id(self, tmp_path):
        result = self._run(
            {"tool_name": "mcp__local-mac__imessage__send"},
            tmp_path=tmp_path,
        )
        assert result == {}



# ---------------------------------------------------------------------------
# tool_usage_logger_lc
# ---------------------------------------------------------------------------

class TestToolUsageLoggerLc:
    def _run(self, hook_input: dict, tool_hints_path: Path,
             checkpoints_db_path: Path | None = None, tmp_path: Path | None = None) -> dict:
        import hooks.dispatcher as hook_mod
        import langchain_learning.nodes.log_tool_usage as tn

        from langchain_learning.config import config as lc_cfg
        mock_cfg = MagicMock()
        mock_cfg.tool_hints_db = tool_hints_path
        mock_cfg.valid_domains = lc_cfg.valid_domains
        mock_cfg.memory_db = lc_cfg.memory_db

        with patch.object(tn, "_cfg", mock_cfg), \
             patch("sys.argv", ["dispatcher.py", "PostToolUse"]), \
             patch("sys.stdin", StringIO(json.dumps(hook_input))), \
             patch("sys.stdout", new_callable=StringIO) as mock_out:
            hook_mod.main()
            out = mock_out.getvalue().strip()

        return json.loads(out) if out else {}

    def test_upserts_new_tool_hint(self, tool_hints_db, tmp_path):
        self._run(
            {"tool_name": "mcp__local-mac__aq__current_dasha", "session_id": "s1",
             "duration_ms": 120, "tool_input": {}, "prompt_id": "p1"},
            tool_hints_db, tmp_path=tmp_path,
        )
        with sqlite3.connect(str(tool_hints_db)) as conn:
            row = conn.execute(
                "SELECT count, keywords FROM mcp_tool_hints WHERE tool_name = 'aq__current_dasha'"
            ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert "astrology" in row[1]

    def test_increments_existing_hint(self, tool_hints_db, tmp_path):
        for _ in range(3):
            self._run(
                {"tool_name": "mcp__local-mac__aq__current_dasha", "session_id": "s1",
                 "duration_ms": 100, "tool_input": {}, "prompt_id": "p1"},
                tool_hints_db, tmp_path=tmp_path,
            )
        with sqlite3.connect(str(tool_hints_db)) as conn:
            row = conn.execute(
                "SELECT count FROM mcp_tool_hints WHERE tool_name = 'aq__current_dasha'"
            ).fetchone()
        assert row[0] == 3

    def test_non_mcp_tool_skipped(self, tool_hints_db, tmp_path):
        self._run(
            {"tool_name": "Bash", "session_id": "s1", "duration_ms": 50},
            tool_hints_db, tmp_path=tmp_path,
        )
        with sqlite3.connect(str(tool_hints_db)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM mcp_tool_hints").fetchone()[0]
        assert count == 0

    def test_memory_tool_skipped(self, tool_hints_db, tmp_path):
        self._run(
            {"tool_name": "mcp__local-mac__memory__search", "session_id": "s1", "duration_ms": 20},
            tool_hints_db, tmp_path=tmp_path,
        )
        with sqlite3.connect(str(tool_hints_db)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM mcp_tool_hints").fetchone()[0]
        assert count == 0

    def test_tool_body_not_retained_in_state(self, tool_hints_db, tmp_path):
        """task:84dbcf14: the session state must hold no copy of a tool body and no
        per-call history, however many calls the turn makes."""
        session_store.begin_turn("sess-1", "search contact")
        self._run(
            {"tool_name": "mcp__local-mac__contacts__search", "session_id": "sess-1",
             "duration_ms": 80, "tool_input": {"content": "BODY" * 500}},
            tool_hints_db, tmp_path=tmp_path,
        )
        values = session_store.get("sess-1")
        assert set(values) == {"turn", "prompt", "prompt_id", "stop_alert_sent", "sound_played"}
        assert "BODY" not in repr(values)

    def test_avg_latency_computed_correctly(self, tool_hints_db, tmp_path):
        for ms in [100, 200]:
            self._run(
                {"tool_name": "mcp__local-mac__contacts__search", "session_id": "s1",
                 "duration_ms": ms, "tool_input": {}, "prompt_id": "p"},
                tool_hints_db, tmp_path=tmp_path,
            )
        with sqlite3.connect(str(tool_hints_db)) as conn:
            row = conn.execute(
                # local-mac calls are learned under their `mac` command id (task:0aa96180)
                "SELECT avg_latency_ms FROM mcp_tool_hints WHERE tool_name = 'mac contacts search'"
            ).fetchone()
        assert row[0] == 150.0


# ---------------------------------------------------------------------------
# stop_hook_lc
# ---------------------------------------------------------------------------

class TestStopHookLc:
    def _run(self, hook_input: dict) -> dict:
        import hooks.dispatcher as hook_mod

        # Stop calls live_claude_sessions() (a `ps` subprocess) for its multi-session
        # audit log. Stub it so these tests stay deterministic regardless of how many
        # `claude` processes the host is running, and so popen.assert_called_once()
        # below counts only the chime.
        with patch("sys.argv", ["dispatcher.py", "Stop"]), \
             patch("sys.stdin", StringIO(json.dumps(hook_input))), \
             patch("hooks.stop_effects.live_claude_sessions", return_value=[]), \
             patch("sys.stdout", new_callable=StringIO) as mock_out:
            hook_mod.main()
            out = mock_out.getvalue().strip()

        return json.loads(out) if out else {}

    def test_stop_hook_first_call_plays_sound_and_returns_empty(self, tmp_path):
        with patch("subprocess.Popen") as popen:
            result = self._run({"session_id": "any-session"})
        assert result == {}
        popen.assert_called_once()

    def test_stop_hook_second_call_same_turn_is_noop(self, tmp_path):
        with patch("subprocess.Popen") as popen:
            first = self._run({"session_id": "any-session"})
            assert first == {}
            popen.assert_called_once()

            second = self._run({"session_id": "any-session"})
            assert second == {}
            popen.assert_called_once()  # still just the one call from the first Stop

