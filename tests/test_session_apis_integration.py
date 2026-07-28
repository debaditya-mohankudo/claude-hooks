"""Live smoke test for GET /session/current and GET /session/active against the
real running hook server — proves the _latest_checkpoint_tuple fix (hooks/
session_state.py) actually resolves the cross-thread staleness bug in production,
not just against mocks.

Background: test_checkpoint_query_integration.py's own fixture posts a hardcoded
session_id ("checkpoint-query-integration-test-session") to the live server every
time it runs. Since Python dict insertion order never changes on key *updates*
(only on first insert), that thread_id became permanently first in MemorySaver's
internal storage dict the first time this suite ever ran — which is exactly what
made the old `next(iter(checkpointer.list(None)))` approach get stuck on it forever,
regardless of how much real traffic ran afterward. This test reproduces that exact
scenario deliberately: post the known-stale session_id, then post a genuinely newer
one, and confirm /session/current returns the newer one — not the older thread that
happens to sort first.

Excluded from the default pytest run (marked `integration`). Requires a live hook
server:
    uv run python -m pytest tests/test_session_apis_integration.py -v
Skips cleanly if no server is reachable.
"""
import json
import time
import urllib.request

import pytest

from src.tools.hooks import _SERVER_URL, handle_session_id

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def live_server():
    try:
        with urllib.request.urlopen(f"{_SERVER_URL}/health", timeout=2):
            pass
    except Exception as exc:
        pytest.skip(f"no live hook server at {_SERVER_URL} ({exc})")


def _post_prompt(session_id: str, prompt: str = "integration smoke test") -> None:
    payload = json.dumps({"session_id": session_id, "cwd": "/tmp", "prompt": prompt}).encode()
    req = urllib.request.Request(
        f"{_SERVER_URL}/hook/UserPromptSubmit", data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5):
        pass


def _get_session_current() -> dict:
    with urllib.request.urlopen(f"{_SERVER_URL}/session/current", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_session_active() -> dict:
    with urllib.request.urlopen(f"{_SERVER_URL}/session/active", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TestSessionCurrentLive:
    def test_does_not_get_stuck_on_the_known_stale_integration_session(self, live_server):
        """Reproduces the exact bug: post the long-lived stale session_id first
        (same one test_checkpoint_query_integration.py's fixture always uses),
        then post a genuinely new session, and confirm /session/current follows
        the new one — not stuck on whichever thread_id was inserted first."""
        _post_prompt("checkpoint-query-integration-test-session")
        time.sleep(0.05)  # ensure a distinct, later checkpoint['ts']
        new_sid = f"fresh-session-{int(time.time() * 1000)}"
        _post_prompt(new_sid)

        result = _get_session_current()
        assert result.get("session_id") == new_sid, (
            f"expected the newest session ({new_sid}), got {result.get('session_id')!r} — "
            "the cross-thread staleness bug may have regressed"
        )

    def test_handle_session_id_mcp_tool_matches_endpoint(self, live_server):
        new_sid = f"fresh-session-{int(time.time() * 1000)}"
        _post_prompt(new_sid)
        result = handle_session_id()
        assert result.get("session_id") == new_sid

    def test_returns_dict_with_turn_field(self, live_server):
        sid = f"fresh-session-{int(time.time() * 1000)}"
        _post_prompt(sid)
        result = _get_session_current()
        assert "turn" in result
        assert isinstance(result["turn"], int)


class TestSessionActiveLive:
    def test_no_error_when_no_task_active(self, live_server):
        # Posting a prompt with no task activation should not blow up /session/active,
        # even though it may legitimately return {} (no active task anywhere).
        sid = f"fresh-session-{int(time.time() * 1000)}"
        _post_prompt(sid)
        result = _get_session_active()
        assert isinstance(result, dict)
