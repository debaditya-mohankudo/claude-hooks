"""Stop-hook side effects: the completion chime and the live-session audit (task:31fb711d).

Both used to be LangGraph nodes (noop, play_sound). They are plain functions the
dispatcher calls on Stop; the once-per-turn guard lives in hooks/session_store.py.
"""
from __future__ import annotations

import subprocess

from src.logger import get_logger

_log = get_logger(__name__)

_SOUND_PATH = "/System/Library/Sounds/Glass.aiff"
_DURATION_SECONDS = 3


def live_claude_sessions() -> list[tuple[str, str]]:
    """List (pid, elapsed) for every running `claude` CLI process.

    Matches on the basename of `comm` (e.g. .../resources/native-binary/claude)
    so MCP server subprocesses (python, node, uv) sharing the word "claude" in
    their path don't get counted. Fails open (empty list): an observability
    nicety on the Stop hot path must never block or slow a turn's Stop response.

    Also backs GET /session/live in hooks/server.py.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,etime=,comm="],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception as exc:
        _log.warning("[stop] live session ps failed: %s", exc)
        return []

    sessions = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid, etime, comm = parts[0], parts[1], parts[-1]
        if comm.rsplit("/", 1)[-1] == "claude":
            sessions.append((pid, etime))
    return sessions


def log_live_sessions_if_multiple() -> None:
    """Log a warning with each pid's elapsed runtime when more than one `claude` CLI is live.

    Surfaces a forgotten detached tmux/terminal session (a 20-day-old stray
    process once turned up through a runaway sound loop) passively on every
    Stop, instead of only when someone queries GET /session/live.
    """
    sessions = live_claude_sessions()
    if len(sessions) <= 1:
        return
    detail = ", ".join(f"pid={pid} etime={etime}" for pid, etime in sessions)
    _log.warning("[stop] live claude sessions=%d: %s", len(sessions), detail)


def play_completion_sound(session_id: str = "") -> None:
    """Fire an `afplay` chime as a detached subprocess.

    start_new_session=True lets the sound outlive this request/response cycle,
    so no delay is needed for it to play before Claude stops. The caller
    (dispatcher._handle_stop) fires it once per turn.
    """
    cmd = (
        f"end=$(($(date +%s)+{_DURATION_SECONDS})); "
        f"while [ $(date +%s) -lt $end ]; do afplay {_SOUND_PATH}; done"
    )
    subprocess.Popen(
        ["sh", "-c", cmd],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _log.info("[stop] chime fired session=%s", session_id[:8])
