#!/usr/bin/env python3
"""Hook client — thin HTTP wrapper for the FastAPI hook server.

Reads hook JSON payload from stdin — Claude Code already includes the real
session cwd in that payload — and POSTs to localhost:8766. CLAUDE_CWD is
only a fallback for the rare case stdin's cwd is missing/empty; it must
never override a cwd Claude Code already supplied (bug found 2026-07-08:
unconditionally overwriting payload["cwd"] with CLAUDE_CWD silently
clobbered the real per-session cwd with an empty/stale env var, making
every session's hook payload report whatever cwd the long-running server
process happened to launch from). Fail-open: if server unreachable, exits 0
with empty JSON.

PreToolUse and PostToolUse are not registered through this client: settings.json
POSTs them to the server as "http" hooks (docs/setup.md).

Usage: python3 client.py <HookEvent>
Events: UserPromptSubmit | PreToolUse | PostToolUse | Stop | SessionStart | SessionEnd
        | Notification (kitty tab status only; not forwarded to the server)
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

EVENT = sys.argv[1] if len(sys.argv) > 1 else ""
if not EVENT:
    print("Usage: client.py <HookEvent>", file=sys.stderr)
    sys.exit(1)

SERVER = os.environ.get("CLAUDE_HOOKS_SERVER", "http://127.0.0.1:8766")
TIMEOUT_S = float(os.environ.get("CLAUDE_HOOKS_CLIENT_TIMEOUT", "2"))


def _log_db() -> str:
    # Same resolution order as src/logger.SQLiteHandler._connect, so a test run
    # (CLAUDE_HOOKS_TEST_LOG_DB) never writes the real log. Stdlib-only: this file
    # cannot import src.config.
    return (os.environ.get("CLAUDE_HOOKS_TEST_LOG_DB")
            or os.path.expanduser("~/.claude/claude_hooks.sqlite"))


def record_failure(event: str, kind: str, detail: str, elapsed_ms: float) -> None:
    """Write a failed POST to hook_logs (logger `hook_client`), where
    `mac hooks read_logs_sqlite --logger hook_client` shows it (task:bc39e90e).

    Stderr alone was invisible: Claude Code does not surface a hook's stderr, so a
    UserPromptSubmit that timed out lost all its injected context without a trace.
    Never raises, and waits at most 0.2 s on a locked DB: this runs on the hook path.
    """
    try:
        import sqlite3
        path = _log_db()
        conn = sqlite3.connect(path, timeout=0.2, uri=path.startswith("file:"))
        try:
            with conn:
                conn.execute(
                    "INSERT INTO hook_logs (logger, level, message) VALUES (?, ?, ?)",
                    ("hook_client", "WARNING",
                     f"{event} {kind} after {elapsed_ms:.0f}ms (timeout={TIMEOUT_S:g}s): {detail[:200]}"),
                )
        finally:
            conn.close()
    except Exception:
        pass


def _failure_kind(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, urllib.error.URLError) or isinstance(exc, ConnectionError):
        return "unreachable"
    return "error"


# Events that only drive the kitty tab; the server has no route for them.
KITTY_ONLY_EVENTS = {"Notification"}

print(f"claude-hooks: client.py invoked for {EVENT}", file=sys.stderr)

try:
    payload = json.load(sys.stdin)
    # Kitty tab status runs here, not on the server: only this process has the
    # session's KITTY_* env. It is fire-and-forget and independent of the POST.
    import kitty_status
    kitty_status.update(EVENT, payload)
    import dashboard_plan
    dashboard_plan.update(EVENT, payload)
    import dashboard_turn
    dashboard_turn.update(EVENT, payload)
    import last_response
    last_response.update(EVENT, payload)
    if EVENT in KITTY_ONLY_EVENTS:
        print("{}")
        sys.exit(0)
    if not payload.get("cwd"):
        payload["cwd"] = os.environ.get("CLAUDE_CWD", "")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{SERVER}/hook/{EVENT}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            print(resp.read().decode())
    except Exception as exc:
        record_failure(EVENT, _failure_kind(exc), str(exc), (time.monotonic() - t0) * 1000)
        raise
except Exception as exc:
    print(f"claude-hooks: server unreachable for {EVENT}, failing open ({exc})", file=sys.stderr)
    print("{}")
