"""API-level integration tests for the FastAPI hook server.

Uses FastAPI TestClient — runs the full ASGI stack in-process (lifespan fires,
MemorySaver graph is built). No real server or port binding, no disk file needed.

Excluded from the default pytest run (marked `integration`). Run explicitly:
    uv run python -m pytest tests/test_server_api.py -v
"""
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).parent.parent
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "hooks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """Single TestClient for the module — lifespan fires once, graph is built.

    task:b3964f85 — MemorySaver is in-process/in-memory, no disk file at all,
    so there's nothing to redirect to a temp path anymore (unlike the old
    SqliteSaver-backed fixture, which pointed _CHECKPOINT_DB at a temp file
    so tests never touched the production checkpoint DB).
    """
    import langchain_learning.session_graph as sg_mod
    from fastapi.testclient import TestClient
    from hooks.server import app

    sg_mod._graph = None
    with TestClient(app) as c:
        yield c
    sg_mod._graph = None


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_status_is_ok(self, client):
        r = client.get("/health")
        assert r.json()["status"] == "ok"

    def test_has_only_status_key(self, client):
        r = client.get("/health")
        assert set(r.json().keys()) >= {"status"}


# ---------------------------------------------------------------------------
# /session
# ---------------------------------------------------------------------------

class TestSession:
    def test_returns_200(self, client):
        r = client.get("/session")
        assert r.status_code == 200

    def test_has_count_and_sessions_keys(self, client):
        r = client.get("/session")
        body = r.json()
        assert "count" in body
        assert "sessions" in body

    def test_sessions_is_list(self, client):
        r = client.get("/session")
        assert isinstance(r.json()["sessions"], list)

    def test_count_matches_sessions_length(self, client):
        r = client.get("/session")
        body = r.json()
        assert body["count"] == len(body["sessions"])


# ---------------------------------------------------------------------------
# /session/{session_id}
# ---------------------------------------------------------------------------

class TestSessionDetail:
    def test_unknown_session_returns_404(self, client):
        r = client.get("/session/does-not-exist-session-id")
        assert r.status_code == 404

    def test_known_session_returns_200(self, client):
        sid = "api-test-session-detail-1"
        client.post("/hook/UserPromptSubmit", json={"session_id": sid, "cwd": "/tmp", "prompt": "hi"})
        r = client.get(f"/session/{sid}")
        assert r.status_code == 200

    def test_has_session_id_turn_count_state_keys(self, client):
        sid = "api-test-session-detail-2"
        client.post("/hook/UserPromptSubmit", json={"session_id": sid, "cwd": "/tmp", "prompt": "hi"})
        r = client.get(f"/session/{sid}")
        body = r.json()
        assert body["session_id"] == sid
        assert "turn_count" in body
        assert "state" in body

    def test_turn_count_increments_across_calls(self, client):
        # turn_count is checkpointer.list()'s write count for the thread, not a
        # simple hook-call tally — UserPromptSubmit's subgraph appends multiple
        # checkpoint entries per call, so two UPS calls reliably increase it.
        sid = "api-test-session-detail-3"
        client.post("/hook/UserPromptSubmit", json={"session_id": sid, "cwd": "/tmp", "prompt": "hi"})
        first = client.get(f"/session/{sid}").json()["turn_count"]
        client.post("/hook/UserPromptSubmit", json={"session_id": sid, "cwd": "/tmp", "prompt": "hi again"})
        second = client.get(f"/session/{sid}").json()["turn_count"]
        assert second > first

    def test_state_is_dict(self, client):
        sid = "api-test-session-detail-4"
        client.post("/hook/UserPromptSubmit", json={"session_id": sid, "cwd": "/tmp", "prompt": "hi"})
        r = client.get(f"/session/{sid}")
        assert isinstance(r.json()["state"], dict)


# ---------------------------------------------------------------------------
# Session lifecycle eviction — Stop must NOT evict; SessionEnd evicts (bug:b7cb4eb4)
# ---------------------------------------------------------------------------

class TestSessionLifecycleEviction:
    def _has_checkpoint(self, sid: str) -> bool:
        import langchain_learning.session_graph as sg
        cfg = {"configurable": {"thread_id": sid}}
        return sg._graph.checkpointer.get(cfg) is not None

    def test_stop_keeps_checkpoint_but_sessionend_evicts(self, client):
        sid = "api-test-evict"
        client.post("/hook/UserPromptSubmit", json={"session_id": sid, "cwd": "/tmp", "prompt": "hi"})
        assert self._has_checkpoint(sid)     # checkpoint created by UPS

        client.post("/hook/Stop", json={"session_id": sid})
        assert self._has_checkpoint(sid)     # Stop fires every turn — must NOT evict

        r = client.post("/hook/SessionEnd", json={"session_id": sid})
        assert r.status_code == 200
        assert not self._has_checkpoint(sid) # SessionEnd is the real close → evicted


# ---------------------------------------------------------------------------
# POST /hook/UserPromptSubmit
# ---------------------------------------------------------------------------

# All test session IDs must start with "api-test-" so LogToolUsageNode skips
# the tool_hints upsert and test calls don't pollute the production hints DB.
_UPS_PAYLOAD = {
    "session_id": "api-test-ups",
    "cwd": "/tmp",
    "prompt": "what is the capital of France?",
}


class TestUserPromptSubmit:
    def test_returns_200(self, client):
        r = client.post("/hook/UserPromptSubmit", json=_UPS_PAYLOAD)
        assert r.status_code == 200

    def test_response_is_json(self, client):
        r = client.post("/hook/UserPromptSubmit", json=_UPS_PAYLOAD)
        assert r.headers["content-type"].startswith("application/json")

    def test_valid_prompt_returns_dict(self, client):
        r = client.post("/hook/UserPromptSubmit", json=_UPS_PAYLOAD)
        assert isinstance(r.json(), dict)

    def test_hookSpecificOutput_shape_when_present(self, client):
        r = client.post("/hook/UserPromptSubmit", json=_UPS_PAYLOAD)
        body = r.json()
        if body:
            assert "hookSpecificOutput" in body
            assert "additionalSystemPrompt" in body["hookSpecificOutput"]
            assert isinstance(body["hookSpecificOutput"]["additionalSystemPrompt"], str)

    def test_empty_prompt_returns_200(self, client):
        r = client.post("/hook/UserPromptSubmit", json={
            "session_id": "api-test-empty",
            "cwd": "/tmp",
            "prompt": "",
        })
        assert r.status_code == 200

    def test_empty_prompt_returns_empty_body(self, client):
        r = client.post("/hook/UserPromptSubmit", json={
            "session_id": "api-test-empty",
            "cwd": "/tmp",
            "prompt": "",
        })
        assert r.json() == {}

    def test_missing_session_id_does_not_raise(self, client):
        r = client.post("/hook/UserPromptSubmit", json={
            "cwd": "/tmp",
            "prompt": "hello",
        })
        assert r.status_code == 200

    def test_prompt_via_message_content(self, client):
        """Prompt can be nested inside message.content list blocks."""
        r = client.post("/hook/UserPromptSubmit", json={
            "session_id": "api-test-msg",
            "cwd": "/tmp",
            "message": {"content": [{"type": "text", "text": "explain recursion"}]},
        })
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# cwd=/tmp → domain=test — fixed memory set (cwd_domains.json maps "/tmp" to
# "test"; test-fixture-alpha/beta are seeded in MEMORY.sqlite under domain=test)
# ---------------------------------------------------------------------------

class TestTmpCwdFixedMemories:
    def test_tmp_cwd_injects_known_test_domain_memories(self, client):
        r = client.post("/hook/UserPromptSubmit", json={
            "session_id": "api-test-tmp-memories",
            "cwd": "/tmp",
            "prompt": "integration test fixture sentinel",
        })
        assert r.status_code == 200
        prompt = r.json().get("hookSpecificOutput", {}).get("additionalSystemPrompt", "")
        assert "test-fixture-alpha" in prompt
        assert "test-fixture-beta" in prompt


# ---------------------------------------------------------------------------
# POST /hook/PreToolUse
# ---------------------------------------------------------------------------

_PTU_SESSION = "api-test-ptu"


@pytest.fixture(scope="module")
def ptu_session(client):
    """Seed a UPS checkpoint so gate has session state to read from."""
    client.post("/hook/UserPromptSubmit", json={
        "session_id": _PTU_SESSION,
        "cwd": "/tmp",
        "prompt": "send a message to alice",
    })


class TestPreToolUse:
    def test_ungated_tool_returns_200(self, client, ptu_session):
        r = client.post("/hook/PreToolUse", json={
            "session_id": _PTU_SESSION,
            "tool_name": "mcp__local-mac__notes__list",
            "tool_input": {},
        })
        assert r.status_code == 200

    def test_ungated_tool_returns_empty(self, client, ptu_session):
        r = client.post("/hook/PreToolUse", json={
            "session_id": _PTU_SESSION,
            "tool_name": "mcp__local-mac__notes__list",
            "tool_input": {},
        })
        assert r.json() == {}

    def test_missing_session_id_fails_open(self, client):
        r = client.post("/hook/PreToolUse", json={
            "tool_name": "mcp__local-mac__imessage__send",
            "tool_input": {"to": "alice", "message": "hi"},
        })
        assert r.status_code == 200
        assert r.json() == {}

    def test_memory_tool_skipped(self, client, ptu_session):
        r = client.post("/hook/PreToolUse", json={
            "session_id": _PTU_SESSION,
            "tool_name": "mcp__local-mac__memory__add",
            "tool_input": {},
        })
        assert r.status_code == 200
        assert r.json() == {}

    def test_gated_tool_denied_without_prereq(self, client):
        """imessage__send denied when no contacts__search prereq in session."""
        r = client.post("/hook/PreToolUse", json={
            "session_id": "api-test-ptu-no-prereq",
            "tool_name": "mcp__local-mac__imessage__send",
            "tool_input": {"to": "alice", "message": "hi"},
        })
        assert r.status_code == 200
        body = r.json()
        assert body.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_deny_response_has_reason(self, client):
        r = client.post("/hook/PreToolUse", json={
            "session_id": "api-test-ptu-no-prereq-2",
            "tool_name": "mcp__local-mac__imessage__send",
            "tool_input": {"to": "alice", "message": "hi"},
        })
        body = r.json()
        reason = body.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
        assert isinstance(reason, str) and len(reason) > 0

    def test_deny_response_has_hook_event_name(self, client):
        r = client.post("/hook/PreToolUse", json={
            "session_id": "api-test-ptu-no-prereq-3",
            "tool_name": "mcp__local-mac__imessage__send",
            "tool_input": {"to": "alice", "message": "hi"},
        })
        body = r.json()
        assert body.get("hookSpecificOutput", {}).get("hookEventName") == "PreToolUse"

    def test_non_mcp_non_bash_tool_passthrough(self, client, ptu_session):
        """Non-MCP, non-Bash tools are not gated — fail open."""
        r = client.post("/hook/PreToolUse", json={
            "session_id": _PTU_SESSION,
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
        })
        assert r.status_code == 200
        assert r.json() == {}


# ---------------------------------------------------------------------------
# POST /hook/PostToolUse
# ---------------------------------------------------------------------------

class TestPostToolUse:
    def test_returns_200(self, client):
        r = client.post("/hook/PostToolUse", json={
            "session_id": "api-test-ptu2",
            "tool_name": "mcp__local-mac__contacts__search",
            "tool_input": {"query": "alice"},
            "tool_response": {"results": []},
            "duration_ms": 42,
        })
        assert r.status_code == 200

    def test_returns_empty_body(self, client):
        """PTU handler never returns blocking output — always empty."""
        r = client.post("/hook/PostToolUse", json={
            "session_id": "api-test-ptu2",
            "tool_name": "mcp__local-mac__contacts__search",
            "tool_input": {"query": "alice"},
            "tool_response": {"results": []},
            "duration_ms": 42,
        })
        assert r.json() == {}

    def test_non_mcp_tool_skipped(self, client):
        r = client.post("/hook/PostToolUse", json={
            "session_id": "api-test-ptu2",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {},
            "duration_ms": 10,
        })
        assert r.status_code == 200
        assert r.json() == {}

    def test_memory_tool_skipped(self, client):
        r = client.post("/hook/PostToolUse", json={
            "session_id": "api-test-ptu2",
            "tool_name": "mcp__local-mac__memory__add",
            "tool_input": {},
            "tool_response": {},
            "duration_ms": 5,
        })
        assert r.status_code == 200
        assert r.json() == {}

    def test_missing_session_id_returns_200(self, client):
        r = client.post("/hook/PostToolUse", json={
            "tool_name": "mcp__local-mac__contacts__search",
            "tool_input": {},
            "tool_response": {},
        })
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# POST /hook/Stop
# ---------------------------------------------------------------------------

class TestStop:
    def test_returns_200(self, client):
        r = client.post("/hook/Stop", json={"session_id": "api-test-stop"})
        assert r.status_code == 200

    def test_returns_empty_body(self, client):
        r = client.post("/hook/Stop", json={"session_id": "api-test-stop"})
        assert r.json() == {}

    def test_missing_session_id_returns_200(self, client):
        r = client.post("/hook/Stop", json={})
        assert r.status_code == 200


class TestSessionStart:
    def test_returns_200(self, client):
        r = client.post("/hook/SessionStart", json={"session_id": "api-test-session-start-1"})
        assert r.status_code == 200

    def test_returns_empty_body(self, client):
        r = client.post("/hook/SessionStart", json={"session_id": "api-test-session-start-2"})
        assert r.json() == {}

    def test_prewarms_checkpoint(self, client):
        sid = "api-test-session-start-prewarm"
        client.post("/hook/SessionStart", json={"session_id": sid})
        from langchain_learning.session_graph import get_session_graph, _config
        state = get_session_graph().get_state(_config(sid))
        assert state is not None and state.metadata is not None, "checkpoint thread should exist after SessionStart"

    def test_second_call_is_resumed(self, client):
        sid = "api-test-session-start-resume"
        client.post("/hook/SessionStart", json={"session_id": sid})
        # second call on same session should not overwrite state
        r = client.post("/hook/SessionStart", json={"session_id": sid})
        assert r.status_code == 200

    def test_missing_session_id_returns_200(self, client):
        r = client.post("/hook/SessionStart", json={})
        assert r.status_code == 200


class TestSessionEnd:
    def test_returns_200(self, client):
        r = client.post("/hook/SessionEnd", json={"session_id": "api-test-session-end-1"})
        assert r.status_code == 200

    def test_returns_empty_body(self, client):
        r = client.post("/hook/SessionEnd", json={"session_id": "api-test-session-end-2"})
        assert r.json() == {}

    def test_evicts_checkpoint(self, client):
        sid = "api-test-session-end-evict"
        client.post("/hook/SessionStart", json={"session_id": sid})
        client.post("/hook/SessionEnd", json={"session_id": sid})
        from langchain_learning.session_graph import get_session_graph, _config
        state = get_session_graph().get_state(_config(sid))
        assert state.metadata is None, "checkpoint should be gone after SessionEnd"

    def test_missing_session_id_returns_200(self, client):
        r = client.post("/hook/SessionEnd", json={})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Malformed request bodies — _safe_json must fail open, not 500
# ---------------------------------------------------------------------------

class TestMalformedRequestBody:
    """task:33419f95 — hook endpoints previously crashed with an unhandled
    JSONDecodeError on an empty/malformed body (found via log audit
    2026-07-26). _safe_json() must fail open to {} instead."""

    def test_empty_body_pre_tool_use_returns_200(self, client):
        r = client.post("/hook/PreToolUse", content=b"", headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json() == {}

    def test_malformed_json_pre_tool_use_returns_200(self, client):
        r = client.post("/hook/PreToolUse", content=b"not json at all", headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json() == {}

    def test_empty_body_user_prompt_submit_returns_200(self, client):
        r = client.post("/hook/UserPromptSubmit", content=b"", headers={"Content-Type": "application/json"})
        assert r.status_code == 200

    def test_malformed_json_post_tool_use_returns_200(self, client):
        r = client.post("/hook/PostToolUse", content=b"{broken", headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json() == {}
