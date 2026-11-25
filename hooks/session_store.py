"""Per-session state the hook server keeps between events (task:31fb711d).

A plain dict per session, owned by the dispatcher. It replaced a LangGraph
StateGraph with a MemorySaver checkpointer, whose four chains held one to three
trivial nodes each while every event paid get_state x2 + invoke + update_state.

Fields, and who reads them:

    turn              UserPromptSubmit counter; GET /session/current
    prompt            the last user prompt; handed to log_tool_usage on PostToolUse
    prompt_id         uuid per UserPromptSubmit; the gate log tag on PreToolUse
    stop_alert_sent   True after the first Stop of a turn; reset by UserPromptSubmit
    sound_played      True once the completion chime fired this turn

In-process only: lost on a server restart, evicted on SessionEnd. Sessions that
never send SessionEnd are bounded by _MAX_SESSIONS (oldest touched is dropped),
so the store cannot grow without limit.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict

# Integration tests use a session_id with this prefix; they share the store but
# never touch real sessions' entries, and tool-hint writes skip them.
TEST_SESSION_PREFIX = "integration-test-"

_MAX_SESSIONS = 200

_lock = threading.Lock()
_sessions: "OrderedDict[str, dict]" = OrderedDict()


def _fresh() -> dict:
    return {"turn": 0, "prompt": "", "prompt_id": "", "stop_alert_sent": False, "sound_played": False}


def _touch(session_id: str) -> dict:
    """The session's dict, created if absent and marked most recently used. Caller holds _lock."""
    state = _sessions.get(session_id)
    if state is None:
        state = _sessions[session_id] = _fresh()
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)
    else:
        _sessions.move_to_end(session_id)
    return state


def begin_turn(session_id: str, prompt: str) -> dict:
    """Start a UserPromptSubmit turn: bump the counter, mint a prompt_id, reset the Stop flags.

    Returns a copy of the new state.
    """
    with _lock:
        state = _touch(session_id)
        state["turn"] += 1
        state["prompt"] = prompt
        state["prompt_id"] = str(uuid.uuid4())
        state["stop_alert_sent"] = False
        state["sound_played"] = False
        return dict(state)


def get(session_id: str) -> dict:
    """A copy of the session's state, or fresh defaults when the session is unknown. Never creates an entry."""
    with _lock:
        state = _sessions.get(session_id)
        return dict(state) if state is not None else _fresh()


def exists(session_id: str) -> bool:
    with _lock:
        return session_id in _sessions


def mark_stop(session_id: str) -> bool:
    """Record a Stop event. True exactly once per turn: the first Stop that has not yet fired the chime."""
    with _lock:
        state = _touch(session_id)
        state["stop_alert_sent"] = True
        if state["sound_played"]:
            return False
        state["sound_played"] = True
        return True


def start(session_id: str) -> bool:
    """SessionStart: create the entry. True if the session is new, False if it already existed."""
    with _lock:
        new = session_id not in _sessions
        _touch(session_id)
        return new


def end(session_id: str) -> bool:
    """SessionEnd: drop the entry. True if one existed."""
    with _lock:
        return _sessions.pop(session_id, None) is not None


def current() -> dict:
    """{session_id, turn} of the most recently touched real session, or {} when none exists."""
    with _lock:
        for session_id in reversed(_sessions):
            if not session_id.startswith(TEST_SESSION_PREFIX):
                return {"session_id": session_id, "turn": _sessions[session_id]["turn"]}
        return {}


def snapshot() -> dict[str, dict]:
    """A copy of every session's state, for the /session debug routes."""
    with _lock:
        return {sid: dict(state) for sid, state in _sessions.items()}


def reset() -> None:
    """Drop every session. Tests and server shutdown."""
    with _lock:
        _sessions.clear()
