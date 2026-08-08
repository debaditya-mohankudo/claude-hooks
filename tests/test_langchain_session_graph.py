"""Tests for Component 2 — SessionGraph (LangGraph StateGraph).

Test strategy:
  - All IO (MEMORY.sqlite, tool_hints.sqlite) uses temp files or
    monkeypatched paths — no dependency on real system DBs.
  - Graph topology is exercised end-to-end via graph.invoke().
  - Individual nodes are also tested in isolation to verify partial-update contract.
"""
import json
import sqlite3
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from langchain_learning.session_state import SessionState
from src.db.schema import MEMORIES_DDL, MCP_TOOL_HINTS_DDL
from langchain_learning.session_graph import (
    build_session_graph,
    run_session,
)
from langchain_learning.nodes.load_memories import LoadMemoriesNode
from langchain_learning.nodes._text_utils import tokenise as _tokenise
from langchain_learning.nodes.score_tools import ScoreToolsNode
from langchain_learning.retrievers import NullMemoryRetriever, NullToolScorer

# Instantiate nodes for direct unit testing
load_memories     = LoadMemoriesNode()
score_tools       = ScoreToolsNode()

# ---------------------------------------------------------------------------
# Fixtures — temp DBs
# ---------------------------------------------------------------------------

def _make_memory_db(rows: list[dict]) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.executescript(MEMORIES_DDL)
    conn.executemany(
        "INSERT INTO memories (name, type, tags, body) VALUES (:name,:type,:tags,:body)",
        rows,
    )
    conn.commit()
    conn.close()
    return Path(tmp.name)


def _make_hints_db(rows: list[dict]) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.executescript(MCP_TOOL_HINTS_DDL)
    conn.executemany(
        "INSERT INTO mcp_tool_hints (tool_name, skill, count, keywords) VALUES (:tool_name,:skill,:count,:keywords)",
        rows,
    )
    conn.commit()
    conn.close()
    return Path(tmp.name)


@pytest.fixture
def memory_db():
    return _make_memory_db([
        {"name": "always-on", "type": "user",
         "tags": "global", "body": "always injected"},
        {"name": "astro-mem", "type": "project",
         "tags": "nakshatra rahu panchang", "body": "astrology data"},
        {"name": "market-mem", "type": "project",
         "tags": "gold nifty fii", "body": "market data"},
        {"name": "vault-mem", "type": "reference",
         "tags": "vault note write", "body": "vault operations"},
    ])


@pytest.fixture
def hints_db():
    return _make_hints_db([
        {"tool_name": "panchang__today",     "skill": "panchang", "count": 20, "keywords": "panchang,nakshatra,tithi"},
        {"tool_name": "market__gold_regime", "skill": "gold",     "count": 15, "keywords": "gold,regime,market"},
        {"tool_name": "imessage__send",      "skill": "imessage", "count": 50, "keywords": "send,message,contact"},
        {"tool_name": "vault__write",        "skill": "vault",    "count": 40, "keywords": "write,save,note,vault"},
    ])


@pytest.fixture
def mock_cfg(memory_db, hints_db):
    """Patch langchain_learning.config.config so all retrievers see the temp DBs.

    CombinationSignalRetriever (used by LoadMemoriesNode) imports config
    locally inside retrieve(), so patching langchain_learning.config.config
    is sufficient — there is no load_memories._cfg module attribute to patch.
    """
    import langchain_learning.config as lc
    cfg = types.SimpleNamespace(
        memory_db=memory_db,
        tool_hints_db=hints_db,
    )
    with patch.object(lc, "config", cfg):
        yield cfg


def _base_state(**overrides) -> SessionState:
    from collections import OrderedDict
    s: SessionState = {
        "event_type": "user_prompt_submit",
        "prompt": "", "cwd": "", "session_id": "", "turn": 0,
        "memories": [],
        "keywords": [],
        "tool_hints": [],
        "current_state": "prompt",
        "tool_name": "", "tool_input": {}, "tool_result": {}, "prompt_id": "",
        "prompt_tools": [], "session_prompt_ids": [], "session_tools": OrderedDict(),
        "session_prompt_texts": {},
        "gate_denied": False, "gate_reason": "",
        "duration_ms": 0.0,
    }
    s.update(overrides)  # type: ignore[arg-type]
    return s


# ---------------------------------------------------------------------------
# _tokenise
# ---------------------------------------------------------------------------

def test_tokenise_basic():
    result = _tokenise("what nakshatra is the moon in")
    assert "nakshatra" in result
    assert "moon" in result


def test_tokenise_strips_short_tokens():
    result = _tokenise("is it ok to go")
    assert "ok" not in result  # len < 4


def test_tokenise_lowercases():
    result = _tokenise("NAKSHATRA RAHU")
    assert "nakshatra" in result
    assert "rahu" in result


# ---------------------------------------------------------------------------
# load_memories node
# ---------------------------------------------------------------------------

def test_load_memories_scores_relevant(mock_cfg):
    # No domain scoping any more — astro-mem wins purely on tag/body overlap.
    result = load_memories(_base_state(prompt="what is my nakshatra today", cwd="/workspace/astrology"))
    names = [m["name"] for m in result["memories"]]
    assert "astro-mem" in names


def test_load_memories_excludes_unrelated_keywords(mock_cfg):
    # No domain scoping any more — market-mem and vault-mem simply don't
    # overlap with these keywords, so they don't surface regardless of cwd.
    result = load_memories(_base_state(prompt="nakshatra moon rising", cwd="/workspace/astrology"))
    names = [m["name"] for m in result["memories"]]
    assert "market-mem" not in names
    assert "vault-mem" not in names


def test_load_memories_global_requires_keyword_overlap(mock_cfg):
    # "always-on" has domain=global, tags="global" — "nakshatra" won't hit it
    result = load_memories(_base_state(prompt="nakshatra rahu panchang", cwd="/workspace/astrology"))
    names = [m["name"] for m in result["memories"]]
    # astro-mem should win; always-on (global, no keyword overlap) should not surface over it
    assert "astro-mem" in names
    # always-on has no keyword match with the prompt — it may or may not surface
    # but it must NOT displace the relevant astro-mem
    assert names.index("astro-mem") < names.index("always-on") if "always-on" in names else True


def test_load_memories_extracts_keywords(mock_cfg):
    result = load_memories(_base_state(prompt="nakshatra rahu panchang today", cwd="/workspace/astrology"))
    assert "nakshatra" in result["keywords"]
    assert "panchang" in result["keywords"]


def test_load_memories_missing_db_returns_empty():
    node = LoadMemoriesNode(retriever=NullMemoryRetriever())
    result = node(_base_state(prompt="test"))
    assert result["memories"] == []


def test_load_memories_caps_at_top_n(hints_db):
    rows = [{"name": f"mem{i}", "type": "user",
             "tags": "message send", "body": "macos tool"} for i in range(15)]
    big_db = _make_memory_db(rows)
    import langchain_learning.config as lc
    cfg = types.SimpleNamespace(memory_db=big_db, tool_hints_db=hints_db)
    with patch.object(lc, "config", cfg):
        result = load_memories(_base_state(prompt="send message to contact", cwd="/workspace/claude-hooks"))
    assert len(result["memories"]) <= 10


# ---------------------------------------------------------------------------
# score_tools node
# ---------------------------------------------------------------------------

def test_score_tools_returns_matching_keyword(mock_cfg):
    result = score_tools(_base_state(keywords=["nakshatra"]))
    tool_names = [h["tool_name"] for h in result["tool_hints"]]
    assert "panchang__today" in tool_names


def test_score_tools_excludes_non_matching(mock_cfg):
    result = score_tools(_base_state(keywords=["panchang"]))
    tool_names = [h["tool_name"] for h in result["tool_hints"]]
    assert "imessage__send" not in tool_names


def test_score_tools_caps_at_five(mock_cfg):
    result = score_tools(_base_state(keywords=["write", "send", "gold", "panchang"]))
    assert len(result["tool_hints"]) <= 5


def test_score_tools_missing_db_returns_empty():
    node = ScoreToolsNode(scorer=NullToolScorer())
    result = node(_base_state(keywords=["send"]))
    assert result["tool_hints"] == []


# ---------------------------------------------------------------------------
# KeywordOverlapScorer — count tie-breaker (task:53c9f817)
# ---------------------------------------------------------------------------

def test_score_tools_count_breaks_ties(mock_cfg):
    # imessage__send (count=50) and vault__write (count=40) each match exactly
    # one of these keywords ("send" / "vault") — equal base score (kw_overlap=1),
    # so the higher-count tool should rank first.
    result = score_tools(_base_state(keywords=["send", "vault"]))
    tool_names = [h["tool_name"] for h in result["tool_hints"]]
    assert tool_names.index("imessage__send") < tool_names.index("vault__write")


def test_score_tools_count_does_not_manufacture_relevance(mock_cfg):
    # imessage__send has count=50 (the highest in the fixture) but its
    # keywords don't overlap with the prompt — it must not leak into
    # results just because of its usage count.
    result = score_tools(_base_state(keywords=["panchang"]))
    tool_names = [h["tool_name"] for h in result["tool_hints"]]
    assert "imessage__send" not in tool_names


# ---------------------------------------------------------------------------
# Full graph — end-to-end via build_session_graph()
# ---------------------------------------------------------------------------

def test_graph_compiles():
    graph = build_session_graph()
    assert graph is not None


def test_graph_invoke_produces_prompt_id(mock_cfg):
    graph = build_session_graph()
    result = graph.invoke(_base_state(
        prompt="what nakshatra is the moon in today",
        session_id="",
    ))
    assert result["prompt_id"] != ""


def test_graph_state_is_immutable_between_nodes(mock_cfg):
    """Each node returns a partial dict; original state dict must not be mutated."""
    initial = _base_state(prompt="nakshatra today", session_id="")
    original_memories = initial["memories"]

    graph = build_session_graph()
    result = graph.invoke(initial)

    assert original_memories == []
    assert len(result["memories"]) > 0


def test_run_session_convenience(mock_cfg):
    with patch("langchain_learning.session_graph._graph", None):
        result = run_session("what is the gold price today")

    assert result["prompt_id"] != ""


# ---------------------------------------------------------------------------
# MemorySaver — turn counter persists across invocations on the same thread
# ---------------------------------------------------------------------------

@pytest.fixture()
def mem_graph(mock_cfg):
    """Inject a fresh MemorySaver-backed session graph for tests that need cross-call state."""
    from langgraph.checkpoint.memory import MemorySaver
    import langchain_learning.session_graph as sg
    prev = sg._graph
    sg._graph = sg.build_session_graph(checkpointer=MemorySaver())
    yield sg
    sg._graph = prev


def test_turn_increments_across_invocations(mem_graph, log_turn):
    """Turn must increment each UserPromptSubmit on the same session thread."""
    sg = mem_graph
    log_turn("turn 1")
    r1 = sg.run_session("hello", session_id="turn-test", cwd="/tmp")
    assert r1["turn"] == 1
    log_turn("turn 2")
    r2 = sg.run_session("hello again", session_id="turn-test", cwd="/tmp")
    assert r2["turn"] == 2
    log_turn("turn 3")
    r3 = sg.run_session("one more", session_id="turn-test", cwd="/tmp")
    assert r3["turn"] == 3


def test_ups_pending_hook_output_surfaced_and_cleared(mem_graph):
    """run_session must return pending_hook_output to the caller but zero the
    checkpoint copy, so it cannot leak into the next PostToolUse response
    (run_post_tool returns whatever is in the field)."""
    sg = mem_graph
    sid = "pho-leak-test"
    sg.run_session("first turn", session_id=sid, cwd="/tmp")

    payload = {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "task:aabbcc closed — run /task-introspection task:aabbcc",
    }}
    sg.get_session_graph().update_state(sg._config(sid), {"pending_hook_output": payload})

    result = sg.run_session("second turn", session_id=sid, cwd="/tmp")
    assert result["pending_hook_output"] == payload

    saved = sg.get_session_graph().get_state(sg._config(sid))
    assert (saved.values.get("pending_hook_output") or {}) == {}

    hook_output = sg.run_post_tool("Read", {"file_path": "/tmp/x"}, sid)
    assert hook_output == {}


def test_thread_isolation(mem_graph):
    """Different session_ids must not share turn state."""
    sg = mem_graph
    sg.run_session("prompt 1", session_id="sess-a", cwd="/tmp")
    ra = sg.run_session("prompt 2", session_id="sess-a", cwd="/tmp")
    rb = sg.run_session("prompt 1", session_id="sess-b", cwd="/tmp")
    assert ra["turn"] == 2
    assert rb["turn"] == 1


# ---------------------------------------------------------------------------
# Cross-hook checkpoint integration tests
# ---------------------------------------------------------------------------

class TestCheckpointCrossHook:
    """Verify that prompt_id and other state flow correctly across all four
    hook invocations (UserPromptSubmit → PreToolUse → PostToolUse → Stop)
    via the MemorySaver checkpoint — no DB reads mid-session.
    """

    # imessage__send integration tests removed here: imessage__send moved to
    # claude_for_mac_local along with the tool itself. mail__compose is now
    # the only prereq gate this repo still exercises end-to-end.

    def test_prompt_id_flows_from_submit_to_gate(self, mem_graph, _log_test_marker, log_turn):
        """prompt_id written by UserPromptSubmit must be readable by PreToolUse via checkpoint."""
        sg = mem_graph
        sid = "chk-test-gate"

        log_turn("user_prompt_submit")
        r1 = sg.run_session("send this to alice@example.com", session_id=sid, cwd="/tmp")
        prompt_id_from_submit = r1["prompt_id"]
        assert prompt_id_from_submit, "UserPromptSubmit must set prompt_id"

        log_turn("post_tool_use")
        sg.run_post_tool("mcp__local-mac__contacts__search", {"name": "Alice"}, session_id=sid, duration_ms=50,
                         tool_result={"name": "Alice", "phoneNumbers": [{"value": "+911234567890"}]})

        log_turn("pre_tool_use gate")
        gate_result = sg.run_gate("mail__compose", {"to": "alice@example.com"}, session_id=sid)

        assert not gate_result["gate_denied"], \
            f"Gate should allow after prereqs; got denied: {gate_result['gate_reason']}"

    def test_prompt_id_not_reset_between_hooks(self, mem_graph):
        """prompt_id must be the same across UserPromptSubmit and all subsequent hooks in the same turn."""
        sg = mem_graph
        sid = "chk-test-stable-pid"

        r1 = sg.run_session("check gold price", session_id=sid, cwd="/tmp")
        prompt_id_t1 = r1["prompt_id"]

        cp_after_gate = sg.get_session_graph().get_state({"configurable": {"thread_id": sid}})
        sg.run_gate("contacts__search", {}, session_id=sid)
        prompt_id_in_gate_checkpoint = cp_after_gate.values.get("prompt_id", "")

        assert prompt_id_in_gate_checkpoint == prompt_id_t1, \
            f"prompt_id changed between submit and gate: {prompt_id_t1!r} → {prompt_id_in_gate_checkpoint!r}"

    def test_new_turn_gets_new_prompt_id(self, mem_graph, log_turn):
        """Each UserPromptSubmit must generate a fresh prompt_id, replacing the prior one."""
        sg = mem_graph
        sid = "chk-test-new-pid"

        log_turn("turn 1")
        r1 = sg.run_session("turn one", session_id=sid, cwd="/tmp")
        pid1 = r1["prompt_id"]
        log_turn("turn 2")
        r2 = sg.run_session("turn two", session_id=sid, cwd="/tmp")
        pid2 = r2["prompt_id"]

        assert pid1 != pid2, "Each turn must produce a distinct prompt_id"
        assert pid1 != "", "Turn 1 prompt_id must be non-empty"
        assert pid2 != "", "Turn 2 prompt_id must be non-empty"

    def test_turn_increments_correctly_across_all_hooks(self, mem_graph, log_turn):
        """turn counter must only increment on UserPromptSubmit, not on tool hooks."""
        sg = mem_graph
        sid = "chk-test-turn-stable"

        log_turn("turn 1 submit")
        r1 = sg.run_session("turn one", session_id=sid, cwd="/tmp")
        assert r1["turn"] == 1

        log_turn("turn 1 gate")
        sg.run_gate("contacts__search", {}, session_id=sid)
        cp_after_gate = sg.get_session_graph().get_state({"configurable": {"thread_id": sid}})
        assert cp_after_gate.values.get("turn") == 1, \
            f"turn should not change during PreToolUse, got {cp_after_gate.values.get('turn')}"

        log_turn("turn 2 submit")
        r2 = sg.run_session("turn two", session_id=sid, cwd="/tmp")
        assert r2["turn"] == 2

    def test_gate_denied_when_no_checkpoint_exists(self, mem_graph, _log_test_marker):
        """If no prior UserPromptSubmit checkpoint exists, gate must still be safe."""
        sg = mem_graph
        sid = "chk-test-no-prior"

        gate_result = sg.run_gate("mail__compose", {"to": "alice@example.com"}, session_id=sid)
        assert gate_result["gate_denied"], \
            "Gate must deny gated tool when no checkpoint exists (no contacts__search recorded)"
        # Fallback prompt_id must be generated so gate logs are traceable (not prompt_id=?)
        rows = _log_test_marker(search="generated fallback prompt_id")
        assert rows, "run_gate must generate a fallback prompt_id when no UPS checkpoint exists"
        fb_pid = rows[0]["message"].split("prompt_id=")[-1].split()[0]
        assert fb_pid.startswith("fb-"), f"fallback prompt_id should start with 'fb-', got {fb_pid!r}"
        gate_rows = _log_test_marker(logger="lc.hooks.gates", search="DENY")
        assert gate_rows, "DENY must appear in gate logs"
        assert "?" not in gate_rows[0]["message"].split("prompt_id=")[-1].split()[0], \
            "gate log must not show prompt_id=?"

    # MailDeleteGate integration tests removed here: mail__delete moved to
    # claude_for_mac_local along with the tool itself.


# TestDeactivateTaskRetrospective removed (task:882d67fa) — DeactivateTaskNode
# and its retrospective-nudge behaviour are gone along with the rest of the
# active-task pipeline; task-framework's own tasks__finish closes the loop now.
