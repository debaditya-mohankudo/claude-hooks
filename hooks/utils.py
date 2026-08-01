#!/usr/bin/env python3
"""
Hook utilities — shared helpers for all Claude Code hooks.

Usage:
    from utils import read_stdin, post_hook, write_json_to_stdout
"""
import json
import sys
import urllib.request
from typing import Any


def read_stdin() -> dict[str, Any]:
    """Parse JSON from stdin — standard hook input.

    Uses strict=False to tolerate raw control characters in tool inputs
    (e.g. bash scripts with unescaped newlines passed by Claude).
    """
    buf = getattr(sys.stdin, "buffer", None)
    raw: str | bytes = buf.read() if buf is not None else sys.stdin.read()
    return json.loads(raw, strict=False)


def post_hook(url: str, payload: dict, *, timeout: float = 3) -> dict[str, Any]:
    """POST JSON payload to a hook endpoint. Returns parsed response body.

    Raises urllib.error.URLError / json.JSONDecodeError on failure —
    callers decide how to handle (log + fallback vs. propagate).
    """
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def write_json_to_stdout(payload: dict | None = None, *, error: str | None = None) -> None:
    """Write a JSON payload to stdout — the standard hook response mechanism.

    Pass error= to surface a failure reason to Claude via hookSpecificOutput.
    """
    if error:
        payload = {"hookSpecificOutput": {"reason": error}}
    print(json.dumps(payload or {}), file=sys.stdout)
