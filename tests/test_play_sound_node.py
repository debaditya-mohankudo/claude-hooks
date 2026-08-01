"""Tests for PlaySoundNode — fires the completion chime as a direct side effect."""
from __future__ import annotations

from unittest.mock import patch

from langchain_learning.nodes.play_sound import PlaySoundNode


def _state(**kwargs) -> dict:
    base = {"event_type": "stop", "session_id": "sess0001", "stop_alert_sent": False, "sound_played": False}
    base.update(kwargs)
    return base


class TestPlaySoundNode:
    def test_does_not_fire_when_stop_alert_not_sent(self):
        with patch("subprocess.Popen") as popen:
            result = PlaySoundNode()(_state(stop_alert_sent=False))
        assert result == {}
        popen.assert_not_called()

    def test_fires_once_when_stop_alert_just_sent(self):
        with patch("subprocess.Popen") as popen:
            result = PlaySoundNode()(_state(stop_alert_sent=True, sound_played=False))
        assert result == {"sound_played": True}
        popen.assert_called_once()
        assert popen.call_args.kwargs.get("start_new_session") is True

    def test_does_not_refire_same_turn(self):
        with patch("subprocess.Popen") as popen:
            result = PlaySoundNode()(_state(stop_alert_sent=True, sound_played=True))
        assert result == {}
        popen.assert_not_called()
