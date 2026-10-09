"""Tests for hooks/kitty_status.py wired through hooks/client.py.

Contract under test (behaviour, not internals):
- a hook event paints only the tab holding this session's window
  (match window_id:$KITTY_WINDOW_ID) over the socket in $KITTY_LISTEN_ON
- prompt/tool events -> working, Stop -> done, Notification -> needs input,
  SessionStart/End -> cleared
- Notification is never forwarded to the hook server
- with no kitty env, a missing `kitten`, or CLAUDE_KITTY_STATUS=0 nothing is
  spawned, and the client's stdout/exit code are unchanged

A stub `kitten` on PATH records its argv to a file. The real binary is spawned
fire-and-forget, so tests poll for the recording instead of reading it at once.
"""
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_CLIENT = str(Path(__file__).parent.parent / "hooks" / "client.py")
_DEAD_SERVER = "http://127.0.0.1:1"  # nothing listens: client fails open


@pytest.fixture
def stub_kitten(tmp_path):
    log = tmp_path / "kitten.log"
    exe = tmp_path / "bin" / "kitten"
    exe.parent.mkdir()
    exe.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe.parent, log


def _run(event, payload, path_dir=None, **env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("KITTY_")}
    base["CLAUDE_HOOKS_SERVER"] = _DEAD_SERVER
    if path_dir is not None:
        base["PATH"] = f"{path_dir}:{base['PATH']}"
    base.update(env)
    return subprocess.run(
        [sys.executable, _CLIENT, event],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=base,
    )


def _calls(log, expected, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.exists():
            lines = log.read_text().splitlines()
            if len(lines) >= expected:
                return lines
        time.sleep(0.05)
    return log.read_text().splitlines() if log.exists() else []


_KITTY = {"KITTY_WINDOW_ID": "7", "KITTY_LISTEN_ON": "unix:/tmp/kitty-123"}


@pytest.mark.parametrize("event,glyph", [
    ("UserPromptSubmit", "● claude"),
    ("PreToolUse", "● claude"),
    ("PostToolUse", "● claude"),
    ("Stop", "✓ claude"),
    ("Notification", "▲ claude"),
])
def test_event_sets_state_on_own_window_tab(stub_kitten, event, glyph):
    path_dir, log = stub_kitten
    _run(event, {"session_id": "s"}, path_dir, **_KITTY)
    lines = _calls(log, 2)
    assert len(lines) == 2
    assert all("--to unix:/tmp/kitty-123" in l and "--match window_id:7" in l for l in lines)
    assert any(f"set-tab-title" in l and glyph in l for l in lines)
    assert any("set-tab-color" in l for l in lines)


def test_idle_prompt_notification_is_done_not_needs_input(stub_kitten):
    path_dir, log = stub_kitten
    _run("Notification", {"notification_type": "idle_prompt"}, path_dir, **_KITTY)
    assert any("✓ claude" in l for l in _calls(log, 2))


@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd"])
def test_session_boundaries_clear_tab(stub_kitten, event):
    path_dir, log = stub_kitten
    _run(event, {"session_id": "s"}, path_dir, **_KITTY)
    lines = _calls(log, 2)
    assert any("set-tab-color" in l and "active_bg=NONE" in l for l in lines)
    assert not any("claude" in l and "set-tab-title" in l for l in lines)


def test_notification_prints_empty_json_and_exits_zero_without_server():
    r = _run("Notification", {"session_id": "s"})
    assert r.returncode == 0
    assert r.stdout.decode().strip() == "{}"
    assert b"server unreachable" not in r.stderr


@pytest.mark.parametrize("env", [
    {},                                                    # not in kitty
    {"KITTY_WINDOW_ID": "7"},                              # no socket
    {"KITTY_LISTEN_ON": "unix:/tmp/kitty-123"},            # no window id
    {**_KITTY, "CLAUDE_KITTY_STATUS": "0"},                # opted out
])
def test_no_kitty_env_or_opt_out_spawns_nothing(stub_kitten, env):
    path_dir, log = stub_kitten
    r = _run("Stop", {"session_id": "s"}, path_dir, **env)
    time.sleep(0.3)
    assert not log.exists()
    assert r.returncode == 0 and r.stdout.decode().strip() == "{}"


def test_missing_kitten_binary_is_silent(tmp_path):
    r = _run("Stop", {"session_id": "s"}, None, PATH=f"{tmp_path}:/usr/bin:/bin", **_KITTY)
    assert r.returncode == 0 and r.stdout.decode().strip() == "{}"


def test_failing_kitten_does_not_change_client_output(tmp_path):
    exe = tmp_path / "kitten"
    exe.write_text("#!/bin/sh\nexit 1\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    r = _run("PreToolUse", {"session_id": "s"}, tmp_path, **_KITTY)
    assert r.returncode == 0 and r.stdout.decode().strip() == "{}"
