"""Live smoke test proving task:b63088a1's test-session isolation actually holds
against the real running hook server.

Background: this whole line of work started with task:a4531510 (hooks__session_id
returning a stale, wrong session) — root-caused to test_checkpoint_query_integration.py's
fixture posting a hardcoded session_id straight into the LIVE PRODUCTION MemorySaver on
every run. Since Python dict insertion order never changes on key updates, that thread
became permanently first in the production checkpointer's storage dict, which is exactly
what made the old `next(iter(checkpointer.list(None)))` approach get stuck on it forever.

task:b63088a1 fixed the root cause by routing TEST_SESSION_PREFIX'd session_ids to an
isolated test graph. With the graph gone (task:31fb711d) the same guarantee comes from
hooks/session_store.py: current() skips TEST_SESSION_PREFIX'd sessions.

This test proves the isolation holds end-to-end: posting test-prefixed traffic must NOT
change what /session/current reports.

Excluded from the default pytest run (marked `integration`). Requires a live hook server:
    uv run python -m pytest tests/test_session_apis_integration.py -v
Skips cleanly if no server is reachable.
"""
import json
import time
import urllib.request

import pytest

from hooks.session_store import TEST_SESSION_PREFIX
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


def _test_sid(label: str) -> str:
    """A TEST_SESSION_PREFIX'd session_id, skipped by session_store.current()
    (task:b63088a1)."""
    return f"{TEST_SESSION_PREFIX}{label}-{int(time.time() * 1000)}"


class TestTestGraphIsolation:
    def test_posting_test_traffic_does_not_change_session_current(self, live_server):
        """The core regression test for task:b63088a1: production's
        /session/current must be unaffected by any amount of test-prefixed
        traffic, proving the isolation is real, not just documented."""
        before = handle_session_id()

        # Post several test-prefixed sessions, deliberately including one
        # crafted to resemble the exact scenario that caused task:a4531510
        # (an "older" thread inserted first, then a "newer" one afterward).
        _post_prompt(_test_sid("older-thread"))
        time.sleep(0.05)
        _post_prompt(_test_sid("newer-thread"))

        after = handle_session_id()
        assert after.get("session_id") == before.get("session_id"), (
            "production /session/current changed after posting only test-prefixed "
            "traffic — the test-session isolation may have regressed"
        )

    def test_test_prefixed_session_still_queryable_by_explicit_id(self, live_server):
        """The debug /session/{id} lookup endpoint (hooks/server.py's
        session_detail) reads session_store by id — a test session is still directly
        queryable by its own id, it's just never reported as current."""
        from src.tools.hooks import handle_checkpoint_query

        sid = _test_sid("lookup-check")
        _post_prompt(sid)
        result = handle_checkpoint_query(thread_id=sid)
        assert "error" not in result, result
        assert result.get("thread_id") == sid


class TestHandleSessionIdLive:
    def test_returns_dict_with_turn_field(self, live_server):
        result = handle_session_id()
        assert isinstance(result, dict)
        assert "turn" in result or "error" in result
