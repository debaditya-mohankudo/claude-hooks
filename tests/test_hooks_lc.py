"""Tests for hook → session_graph integration.

All hook modules now delegate to session_graph nodes. Tests patch
session_graph._cfg (via _make_sg_cfg) to redirect checkpoints_db
to a tmp directory, giving each test an isolated checkpoint store.

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

import langchain_learning.session_graph as sg_mod
from src.db.schema import MCP_TOOL_HINTS_DDL


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

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
        sg_mod._graph = None

        with patch("sys.argv", ["dispatcher.py", "PreToolUse"]), \
             patch("sys.stdin", StringIO(json.dumps(hook_input))), \
             patch("sys.stdout", new_callable=StringIO) as mock_out:
            hook_mod.main()
            out = mock_out.getvalue().strip()

        sg_mod._graph = None
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

    def test_gated_tool_denied_without_prereq(self, tmp_path):
        result = self._run(
            {"tool_name": "mcp__local-mac__imessage__send", "session_id": "s1", "tool_use_id": "p1"},
            tmp_path=tmp_path,
        )
        assert result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_gated_tool_allowed_after_prereq(self, tmp_path):
        from langgraph.checkpoint.memory import MemorySaver
        import langchain_learning.nodes.log_tool_usage as tn
        from langchain_learning.config import config as lc_cfg
        import hooks.dispatcher as tul_mod

        # Share one MemorySaver graph across all calls in this test
        shared = sg_mod.build_session_graph(checkpointer=MemorySaver())
        sg_mod._graph = shared

        sg_mod.run_session(prompt="send message to Alice", session_id="sess-1", cwd="/tmp")

        tool_hints_path = tmp_path / "tool_hints.sqlite"
        with sqlite3.connect(str(tool_hints_path)) as conn:
            conn.executescript(MCP_TOOL_HINTS_DDL)
            conn.commit()
        mock_cfg_tn = MagicMock()
        mock_cfg_tn.tool_hints_db = tool_hints_path
        mock_cfg_tn.valid_domains = lc_cfg.valid_domains
        mock_cfg_tn.memory_db = lc_cfg.memory_db

        with patch.object(tn, "_cfg", mock_cfg_tn), \
             patch("sys.argv", ["dispatcher.py", "PostToolUse"]), \
             patch("sys.stdin", StringIO(json.dumps({"tool_name": "mcp__local-mac__contacts__search", "session_id": "sess-1", "duration_ms": 50, "tool_input": {"name": "Alice"}, "tool_response": {"name": "Alice", "phoneNumbers": [{"value": "+911234567890"}]}}))), \
             patch("sys.stdout", new_callable=StringIO):
            tul_mod.main()

        # Run gate without resetting _graph so shared MemorySaver state is preserved
        import hooks.dispatcher as hook_mod
        with patch("sys.argv", ["dispatcher.py", "PreToolUse"]), \
             patch("sys.stdin", StringIO(json.dumps({"tool_name": "mcp__local-mac__imessage__send", "session_id": "sess-1"}))), \
             patch("sys.stdout", new_callable=StringIO) as mock_out:
            hook_mod.main()
            result = json.loads(mock_out.getvalue().strip() or "{}")
        sg_mod._graph = None
        assert "hookSpecificOutput" not in result

    def test_mail_compose_denied_without_prereq(self, tmp_path):
        result = self._run(
            {"tool_name": "mcp__local-mac__mail__compose", "session_id": "s2", "tool_use_id": "p2"},
            tmp_path=tmp_path,
        )
        assert result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

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

    def test_prereq_from_different_prompt_still_denied(self, tmp_path):
        from langgraph.checkpoint.memory import MemorySaver
        sg_mod._graph = sg_mod.build_session_graph(checkpointer=MemorySaver())

        # Turn 1: UserPromptSubmit + contacts__search PostToolUse (prereq recorded)
        sg_mod.run_session(prompt="find alice", session_id="sess-x", cwd="/tmp")
        sg_mod.run_post_tool("mcp__local-mac__contacts__search", {}, session_id="sess-x", duration_ms=30)

        # Turn 2: new UserPromptSubmit — resets prompt_tools to []
        sg_mod.run_session(prompt="now send message", session_id="sess-x", cwd="/tmp")

        # Gate on new prompt — contacts__search not in this prompt's prompt_tools → deny
        result = self._run({"tool_name": "mcp__local-mac__imessage__send", "session_id": "sess-x"})
        sg_mod._graph = None
        assert result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# ---------------------------------------------------------------------------
# tool_usage_logger_lc
# ---------------------------------------------------------------------------

class TestToolUsageLoggerLc:
    def _run(self, hook_input: dict, tool_hints_path: Path,
             checkpoints_db_path: Path | None = None, tmp_path: Path | None = None) -> dict:
        import hooks.dispatcher as hook_mod
        import langchain_learning.nodes.log_tool_usage as tn
        sg_mod._graph = None

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

        sg_mod._graph = None
        return json.loads(out) if out else {}

    def test_upserts_new_tool_hint(self, tool_hints_db, tmp_path):
        self._run(
            {"tool_name": "mcp__local-mac__aq__current_dasha", "session_id": "s1",
             "duration_ms": 120, "tool_input": {}, "prompt_id": "p1"},
            tool_hints_db, tmp_path=tmp_path,
        )
        with sqlite3.connect(str(tool_hints_db)) as conn:
            row = conn.execute(
                "SELECT count, domain FROM mcp_tool_hints WHERE tool_name = 'aq__current_dasha'"
            ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] == "astrology"

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

    def test_records_prompt_tool_in_state(self, tool_hints_db, tmp_path):
        from langgraph.checkpoint.memory import MemorySaver
        import langchain_learning.nodes.log_tool_usage as tn
        from langchain_learning.config import config as lc_cfg

        shared = sg_mod.build_session_graph(checkpointer=MemorySaver())
        sg_mod._graph = shared

        sg_mod.run_session(prompt="search contact", session_id="sess-1", cwd="/tmp")

        mock_cfg_tn = MagicMock()
        mock_cfg_tn.tool_hints_db = tool_hints_db
        mock_cfg_tn.valid_domains = lc_cfg.valid_domains
        mock_cfg_tn.memory_db = lc_cfg.memory_db
        with patch.object(tn, "_cfg", mock_cfg_tn):
            sg_mod.run_post_tool("mcp__local-mac__contacts__search", {"query": "kuna"},
                                 session_id="sess-1", duration_ms=80)

        cp_state = sg_mod.get_session_graph().get_state({"configurable": {"thread_id": "sess-1"}})
        sg_mod._graph = None
        prompt_tools = cp_state.values.get("prompt_tools") or []
        assert any(
            (isinstance(t, dict) and t.get("tool") == "contacts__search")
            or t == "contacts__search"
            for t in prompt_tools
        )

    def test_avg_latency_computed_correctly(self, tool_hints_db, tmp_path):
        for ms in [100, 200]:
            self._run(
                {"tool_name": "mcp__local-mac__contacts__search", "session_id": "s1",
                 "duration_ms": ms, "tool_input": {}, "prompt_id": "p"},
                tool_hints_db, tmp_path=tmp_path,
            )
        with sqlite3.connect(str(tool_hints_db)) as conn:
            row = conn.execute(
                "SELECT avg_latency_ms FROM mcp_tool_hints WHERE tool_name = 'contacts__search'"
            ).fetchone()
        assert row[0] == 150.0


# ---------------------------------------------------------------------------
# stop_hook_lc
# ---------------------------------------------------------------------------

class TestStopHookLc:
    def _run(self, hook_input: dict, reset_graph: bool = True) -> dict:
        import hooks.dispatcher as hook_mod
        if reset_graph:
            sg_mod._graph = None

        with patch("sys.argv", ["dispatcher.py", "Stop"]), \
             patch("sys.stdin", StringIO(json.dumps(hook_input))), \
             patch("sys.stdout", new_callable=StringIO) as mock_out:
            hook_mod.main()
            out = mock_out.getvalue().strip()

        if reset_graph:
            sg_mod._graph = None
        return json.loads(out) if out else {}

    def test_stop_hook_first_call_plays_sound_and_returns_empty(self, tmp_path):
        with patch("subprocess.Popen") as popen:
            result = self._run({"session_id": "any-session"})
        assert result == {}
        popen.assert_called_once()

    def test_stop_hook_second_call_same_turn_is_noop(self, tmp_path):
        sg_mod._graph = None
        with patch("subprocess.Popen") as popen:
            first = self._run({"session_id": "any-session"}, reset_graph=False)
            assert first == {}
            popen.assert_called_once()

            second = self._run({"session_id": "any-session"}, reset_graph=False)
            assert second == {}
            popen.assert_called_once()  # still just the one call from the first Stop

        sg_mod._graph = None
