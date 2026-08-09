"""SessionBrief — writes a short summary of a conversation segment to the
Obsidian vault's daily note when that segment ends.

/clear does not reuse the current session_id: it ends the old session (a
SessionEnd hook fires for it) and a new session then starts (a SessionStart
hook fires with a freshly-minted session_id). open() is called from
dispatcher._handle_session_start with the *new* session's id, marking a
segment-start cursor; close() is called from dispatcher._handle_session_end
with the *old* (ending) session's id — open() and close() are always called
with different session_ids for a given /clear, one segment apart. close()
reads hooks/server_memory events since that session's cursor, and — if any
exist — launches a detached subprocess (hooks/session_brief_worker.py) that
summarizes them via Haiku and appends the brief to today's daily note. The
subprocess launch is fire-and-forget so close() never blocks the SessionEnd
response on an LLM call.

The cursor lives in a module-level dict, not on disk: hooks/server.py is a
single long-running uvicorn process, so a session's open() and its later
close() share process memory (same pattern as dispatcher.py's
_CONTEXT_NUDGE_SHOWN_BAND). A server restart between the two loses the
cursor — close() treats that as "nothing to summarize" rather than guessing
a start time (fail open, matches the repo's degrade-and-continue philosophy).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from src.logger import get_logger

_log = get_logger(__name__)

_CURSORS: dict[str, float] = {}

_WORKER_SCRIPT = Path(__file__).resolve().parent / "session_brief_worker.py"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# How many recent server_memory events to scan for this session's segment.
# ServerMemory's own rolling cap (_MAX_ENTRIES) is 1000; scanning that many
# covers any segment without needing a dedicated "since ts" query.
_SCAN_EVENTS = 1000

# Segments smaller than this (by token count of event content) aren't worth a
# Haiku call or a vault entry — a couple of one-line prompts don't need a brief.
_MIN_SEGMENT_TOKENS = 200


class SessionBrief:
    """Context manager: open() on SessionStart (new session_id), close() on
    SessionEnd (the ending session's own session_id) — not the same id."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def open(self) -> None:
        if not self.session_id:
            return
        _CURSORS[self.session_id] = time.time()
        _log.info("[session_brief] open: session=%s", self.session_id[:8])

    def close(self) -> None:
        if not self.session_id:
            return
        cursor = _CURSORS.pop(self.session_id, None)
        if cursor is None:
            _log.info(
                "[session_brief] close skip: session=%s no cursor (never opened or server restarted)",
                self.session_id[:8],
            )
            return

        try:
            from hooks.server_memory import get_server_memory

            events = get_server_memory(n_events=_SCAN_EVENTS).get("events", [])
        except Exception as exc:
            _log.warning("[session_brief] close: failed to read server_memory: %s", exc)
            return

        segment = [
            e for e in events
            if e.get("claude_session_id") == self.session_id and e.get("ts", 0) >= cursor
        ]
        if not segment:
            _log.info("[session_brief] close skip: session=%s no events in segment", self.session_id[:8])
            return

        if not self._segment_is_worth_summarizing(segment):
            _log.info(
                "[session_brief] close skip: session=%s segment under %d-token floor",
                self.session_id[:8], _MIN_SEGMENT_TOKENS,
            )
            return

        self._launch_worker(segment)

    @staticmethod
    def _segment_is_worth_summarizing(segment: list[dict]) -> bool:
        """Skip trivially short segments — not worth a Haiku call or a vault entry."""
        try:
            from src.tools.tokens import count_tokens

            text = " ".join(str(e.get("content", "")) for e in segment)
            return count_tokens(text) >= _MIN_SEGMENT_TOKENS
        except Exception as exc:
            _log.warning("[session_brief] token count failed, defaulting to summarize: %s", exc)
            return True

    def _launch_worker(self, segment: list[dict]) -> None:
        try:
            fd, path = tempfile.mkstemp(prefix="session_brief_", suffix=".json")
            with open(fd, "w", encoding="utf-8") as f:
                json.dump({"session_id": self.session_id, "events": segment}, f)
        except Exception as exc:
            _log.warning("[session_brief] close: failed to stage events file: %s", exc)
            return

        try:
            subprocess.Popen(
                [sys.executable, str(_WORKER_SCRIPT), path],
                cwd=str(_PROJECT_ROOT),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _log.info(
                "[session_brief] close: launched worker session=%s events=%d",
                self.session_id[:8], len(segment),
            )
        except Exception as exc:
            _log.warning("[session_brief] close: failed to launch worker: %s", exc)

    def __enter__(self) -> "SessionBrief":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
