"""Tests for hooks/session_brief.py — cursor lifecycle and event filtering.

close() launching a detached subprocess is asserted via a mocked
subprocess.Popen (same convention as test_play_sound_node.py) so tests never
spawn a real Haiku call or touch the vault.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "hooks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hooks.server_memory as sm
from hooks.session_brief import SessionBrief, _CURSORS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sm.ServerMemory, "_DB", tmp_path / "server_memory.sqlite")
    monkeypatch.setattr(sm.ServerMemory, "_cache", [])
    _CURSORS.clear()
    yield
    _CURSORS.clear()


# ── cursor open/close roundtrip ───────────────────────────────────────────────

def test_open_sets_cursor_for_session():
    SessionBrief("s1").open()
    assert "s1" in _CURSORS


def test_close_pops_cursor():
    SessionBrief("s1").open()
    with patch("subprocess.Popen"):
        SessionBrief("s1").close()
    assert "s1" not in _CURSORS


def test_close_without_open_is_a_noop():
    with patch("subprocess.Popen") as popen:
        SessionBrief("never-opened").close()
    popen.assert_not_called()


def test_close_with_no_session_id_is_a_noop():
    with patch("subprocess.Popen") as popen:
        SessionBrief("").close()
    popen.assert_not_called()


# ── event filtering ───────────────────────────────────────────────────────────

def test_close_skips_write_when_no_events_recorded():
    SessionBrief("s1").open()
    with patch("subprocess.Popen") as popen:
        SessionBrief("s1").close()
    popen.assert_not_called()


def test_close_only_includes_events_for_this_session_after_cursor():
    sm.record_prompt("other-session", "unrelated prompt")

    SessionBrief("s1").open()
    time.sleep(0.01)
    sm.record_prompt("s1", "do the thing " * 100)  # over the 200-token floor
    sm.record_tool("s1", "Read", args="foo.py")

    with patch("subprocess.Popen") as popen:
        SessionBrief("s1").close()

    popen.assert_called_once()
    assert popen.call_args.kwargs.get("start_new_session") is True


def test_close_skips_trivially_short_segment():
    """A segment under the 200-token floor isn't worth a Haiku call."""
    SessionBrief("s1").open()
    sm.record_prompt("s1", "hi")

    with patch("subprocess.Popen") as popen:
        SessionBrief("s1").close()

    popen.assert_not_called()


def test_close_fires_for_segment_over_token_floor():
    SessionBrief("s1").open()
    sm.record_prompt("s1", "word " * 300)  # comfortably over the 200-token floor

    with patch("subprocess.Popen") as popen:
        SessionBrief("s1").close()

    popen.assert_called_once()


def test_close_excludes_events_recorded_before_open():
    sm.record_prompt("s1", "before the segment started")
    time.sleep(0.01)
    SessionBrief("s1").open()

    with patch("subprocess.Popen") as popen:
        SessionBrief("s1").close()

    popen.assert_not_called()


def test_close_does_not_block_on_detached_process():
    """close() must return immediately — it never waits on the worker subprocess."""
    SessionBrief("s1").open()
    sm.record_prompt("s1", "hello " * 250)  # over the 200-token floor

    with patch("subprocess.Popen") as popen:
        SessionBrief("s1").close()

    # Popen (non-blocking) was used, never .wait()/.communicate() on the result.
    popen.assert_called_once()
    popen.return_value.wait.assert_not_called()
    popen.return_value.communicate.assert_not_called()
