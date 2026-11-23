"""Hook routes run their handlers off the event loop, serialised per session (task:bc39e90e).

Before: every route was `async def` calling a synchronous handler, so a slow
UserPromptSubmit (~0.7-2 s) blocked every other session's PreToolUse until it
finished. These tests drive the ASGI app in-process with stubbed handlers; no
lifespan, no graph, no real databases (session ids carry the `test-` prefix that
server_memory skips).
"""
from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

import hooks.dispatcher as dispatcher
from hooks.server import app

_SLOW_S = 0.6


def _run(coro):
    """Run on a private loop: asyncio.run() would clear the main thread's current
    loop, which breaks later tests that still call asyncio.get_event_loop()."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _gather(*calls, raise_app_exceptions=True):
    """Send `calls` 50 ms apart; each result is (response, seconds since the batch started).

    Timed from one shared start, not per request: a blocked event loop also delays
    the moment a request's own timer would start, which hid the stall."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    start = time.perf_counter()
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async def delayed(i, event, sid):
            await asyncio.sleep(0.05 * i)
            r = await client.post(f"/hook/{event}", json={"session_id": sid, "prompt": "hello there"})
            return r, time.perf_counter() - start
        return await asyncio.gather(*(delayed(i, ev, sid) for i, (ev, sid) in enumerate(calls)))


def test_slow_prompt_in_one_session_does_not_delay_other_sessions(monkeypatch):
    monkeypatch.setattr(dispatcher, "_handle_user_prompt_submit", lambda body: time.sleep(_SLOW_S) or {})
    monkeypatch.setattr(dispatcher, "_handle_pre_tool_use", lambda body: {})
    (ups, ups_s), (pre, pre_s) = _run(_gather(
        ("UserPromptSubmit", "test-slow"),
        ("PreToolUse", "test-other"),
    ))
    assert ups.status_code == pre.status_code == 200
    assert ups_s >= _SLOW_S
    assert pre_s < _SLOW_S / 2, f"PreToolUse waited {pre_s:.2f}s behind another session's prompt"


def test_same_session_hooks_never_overlap(monkeypatch):
    active, overlaps = [0], []
    guard = threading.Lock()

    def handler(body):
        with guard:
            active[0] += 1
            overlaps.append(active[0])
        time.sleep(0.15)
        with guard:
            active[0] -= 1
        return {}

    monkeypatch.setattr(dispatcher, "_handle_pre_tool_use", handler)
    results = _run(_gather(*[("PreToolUse", "test-same")] * 3))
    assert all(r.status_code == 200 for r, _ in results)
    assert max(overlaps) == 1


def test_handler_error_is_still_a_500(monkeypatch):
    """hooks/client.py turns a 500 into allow; the threadpool must not swallow it."""
    def boom(body):
        raise RuntimeError("boom")
    monkeypatch.setattr(dispatcher, "_handle_pre_tool_use", boom)
    [(r, _)] = _run(_gather(("PreToolUse", "test-err"), raise_app_exceptions=False))
    assert r.status_code == 500


def test_session_end_drops_the_session_lock(monkeypatch):
    import hooks.server as server
    monkeypatch.setattr(dispatcher, "_handle_pre_tool_use", lambda body: {})
    monkeypatch.setattr(dispatcher, "_handle_session_end", lambda body: None)
    _run(_gather(("PreToolUse", "test-end"), ("SessionEnd", "test-end")))
    assert "test-end" not in server._session_locks
