"""Keep the last assistant response in a plain file, overwritten on every Stop.

Called from client.py. Text comes from the payload's last_assistant_message or
else the transcript's last assistant entry. Path is ~/.claude/last_response.md;
CLAUDE_LAST_RESPONSE overrides it, =0 disables. Never raises, never blocks.
"""
import os
import tempfile
from pathlib import Path

from dashboard_turn import last_assistant_text

_DEFAULT = "~/.claude/last_response.md"


def _path() -> Path | None:
    p = os.environ.get("CLAUDE_LAST_RESPONSE", _DEFAULT)
    return None if p == "0" else Path(p).expanduser()


def update(event: str, payload: dict | None = None) -> None:
    try:
        path = _path()
        if path is None or event != "Stop" or not payload:
            return
        text = (payload.get("last_assistant_message")
                or last_assistant_text(payload.get("transcript_path", "")))
        if not text:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as f:
            f.write(text.rstrip() + "\n")
        os.replace(f.name, path)
    except Exception:
        pass
