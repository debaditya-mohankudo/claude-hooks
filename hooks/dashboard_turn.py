"""Mirror the current turn (prompt + final summary) for the kitty dashboard.

Called from client.py next to dashboard_plan. UserPromptSubmit stores the
prompt and clears the summary; Stop stores the last assistant text, taken from
the payload's last_assistant_message or else the transcript's last assistant
entry. Hook-owned file (~/.claude/dashboard_turn.json, CLAUDE_DASHBOARD_TURN
overrides; =0 disables). Never raises, never blocks.
"""
import json
import os
import tempfile
from pathlib import Path

_DEFAULT = "~/.claude/dashboard_turn.json"


def _path() -> Path | None:
    p = os.environ.get("CLAUDE_DASHBOARD_TURN", _DEFAULT)
    return None if p == "0" else Path(p).expanduser()


def last_assistant_text(transcript: str) -> str:
    """Text of the last assistant message in a JSONL transcript, '' if none."""
    last = ""
    try:
        with open(transcript) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                msg = e.get("message") or {}
                if e.get("type") != "assistant" and msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
                else:
                    text = "\n".join(b.get("text", "") for b in content or []
                                     if isinstance(b, dict) and b.get("type") == "text")
                if text.strip():
                    last = text.strip()
    except OSError:
        pass
    return last


def update(event: str, payload: dict | None = None) -> None:
    try:
        path = _path()
        if path is None or not payload or event not in ("UserPromptSubmit", "Stop"):
            return
        try:
            turn = json.loads(path.read_text())
        except (OSError, ValueError):
            turn = {}
        if event == "UserPromptSubmit":
            turn = {"prompt": payload.get("prompt", ""), "summary": ""}
        else:
            turn["summary"] = (payload.get("last_assistant_message")
                               or last_assistant_text(payload.get("transcript_path", "")))
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as f:
            json.dump(turn, f)
        os.replace(f.name, path)
    except Exception:
        pass
