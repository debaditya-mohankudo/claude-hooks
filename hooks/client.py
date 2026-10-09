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

Usage: python3 client.py <HookEvent>
Events: UserPromptSubmit | PreToolUse | PostToolUse | Stop | SessionStart | SessionEnd
        | Notification (kitty tab status only; not forwarded to the server)
"""
import json
import os
import sys
import urllib.error
import urllib.request

EVENT = sys.argv[1] if len(sys.argv) > 1 else ""
if not EVENT:
    print("Usage: client.py <HookEvent>", file=sys.stderr)
    sys.exit(1)

SERVER = os.environ.get("CLAUDE_HOOKS_SERVER", "http://127.0.0.1:8766")

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
    with urllib.request.urlopen(req, timeout=2) as resp:
        print(resp.read().decode())
except Exception as exc:
    print(f"claude-hooks: server unreachable for {EVENT}, failing open ({exc})", file=sys.stderr)
    print("{}")
