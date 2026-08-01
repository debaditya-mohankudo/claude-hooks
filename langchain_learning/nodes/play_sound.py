"""PlaySoundNode — plays a completion chime directly on the Stop event, no Claude round-trip."""
from __future__ import annotations

import subprocess

from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)

_SOUND_PATH = "/System/Library/Sounds/Glass.aiff"
_DURATION_SECONDS = 3


class PlaySoundNode:
    """Fires an `afplay` chime as a direct side effect of the Stop event.

    Same detached-subprocess trick as the local-mac MCP `time__play_sound` tool
    (start_new_session=True) -- the sound process outlives this request/response
    cycle, so no delay is needed for it to actually play before Claude stops.

    Runs once per turn (guarded by stop_alert_sent, set by NoopNode) so the extra
    Stop event that follows Claude's own turn-end doesn't replay the sound.

    Tags: stop-event, sound-alert, side-effect, no-block
    """

    def __call__(self, state: SessionState) -> dict:
        entry("play_sound", state, session=(state.get("session_id") or "")[:8])

        if not state.get("stop_alert_sent"):
            return {}

        if state.get("sound_played"):
            return {}

        cmd = (
            f"end=$(($(date +%s)+{_DURATION_SECONDS})); "
            f"while [ $(date +%s) -lt $end ]; do afplay {_SOUND_PATH}; done"
        )
        subprocess.Popen(
            ["sh", "-c", cmd],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log.info("[play_sound] fired session=%s", (state.get("session_id") or "")[:8])

        return {"sound_played": True}
