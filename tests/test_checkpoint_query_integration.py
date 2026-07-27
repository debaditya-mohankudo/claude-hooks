"""Live smoke test for handle_checkpoint_query against the real running hook server.

Unlike test_tools_hooks.py's TestHandleCheckpointQuery (which mocks urllib.request.urlopen
entirely), this hits the actual server process on _SERVER_URL with no mocking — it's the
only test that would have caught task:da29c842's bug (handle_checkpoint_query silently
reading a retired, empty sqlite file while the live server had moved to MemorySaver years
apart from what the mocked unit tests exercised).

Excluded from the default pytest run (marked `integration`). Requires a live hook server:
    uv run python -m pytest tests/test_checkpoint_query_integration.py -v
Skips cleanly if no server is reachable at src.tools.hooks._SERVER_URL.
"""
import urllib.request

import pytest

from src.tools.hooks import _SERVER_URL, handle_checkpoint_query

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def live_server():
    """Skip the module if no real hook server is reachable."""
    try:
        with urllib.request.urlopen(f"{_SERVER_URL}/health", timeout=2):
            pass
    except Exception as exc:
        pytest.skip(f"no live hook server at {_SERVER_URL} ({exc})")


@pytest.fixture
def live_session(live_server):
    """Post a real UserPromptSubmit to the live server, returning its session_id."""
    import json

    sid = "checkpoint-query-integration-test-session"
    payload = json.dumps({"session_id": sid, "cwd": "/tmp", "prompt": "integration smoke test"}).encode()
    req = urllib.request.Request(
        f"{_SERVER_URL}/hook/UserPromptSubmit", data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5):
        pass
    return sid


class TestCheckpointQueryLive:
    def test_explicit_session_id_returns_no_error(self, live_session):
        result = handle_checkpoint_query(thread_id=live_session)
        assert "error" not in result, result

    def test_explicit_session_id_matches_thread_id(self, live_session):
        result = handle_checkpoint_query(thread_id=live_session)
        assert result["thread_id"] == live_session

    def test_unknown_session_id_returns_error(self, live_server):
        result = handle_checkpoint_query(thread_id="definitely-not-a-real-session-id")
        assert "error" in result

    def test_default_thread_id_does_not_crash(self, live_server):
        # No thread_id -> resolves via GET /session/current. May or may not find one
        # depending on what else is live, but must never raise or return a raw traceback.
        result = handle_checkpoint_query()
        assert isinstance(result, dict)
