#!/usr/bin/env python3
"""Detached worker for SessionBrief.close() — never invoked directly by a hook.

Launched via `subprocess.Popen([sys.executable, __file__, events_json_path],
start_new_session=True)` from hooks/session_brief.py so the /clear response
never waits on it. Everything in here runs after the parent hook call has
already returned, so nothing here can propagate a failure anywhere except
this process's own log — every step is wrapped so one failure (Haiku call,
vault write) can't orphan the temp file or produce a stack trace nobody
reads.

Usage: session_brief_worker.py <path to json file: {"session_id", "events"}>
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hooks.paths import VAULT_ROOT
from src.logger import get_logger

_log = get_logger(__name__)

_SUMMARY_HEADING = "## Session Summary"

_SUMMARIZE_PROMPT = (
    "Summarize this Claude Code conversation segment into a short brief "
    "(3-6 bullet points) covering what the user asked for and what was "
    "done — tool calls included. Plain markdown bullets, no preamble, no "
    "heading.\n\nEvents:\n{events_text}"
)


def _events_text(events: list[dict]) -> str:
    lines = []
    for ev in events:
        etype = ev.get("type", "?")
        content = ev.get("content", "")
        if etype == "tool":
            args = ev.get("args") or ""
            lines.append(f"- [tool] {content} {args}".strip())
        elif etype == "prompt":
            lines.append(f"- [prompt] {content}")
        elif etype == "task":
            lines.append(f"- [task] {content}")
        else:
            lines.append(f"- [{etype}] {content}")
    return "\n".join(lines)


def _summarize(events: list[dict]) -> str | None:
    try:
        from langchain_learning.subagent import BareClaudeAgent

        agent = BareClaudeAgent(model="claude-haiku-4-5-20251001")
        return agent.invoke(_SUMMARIZE_PROMPT.format(events_text=_events_text(events))).strip()
    except Exception as exc:
        _log.warning("[session_brief_worker] summarize failed: %s", exc)
        return None


def _append_to_daily_note(brief: str) -> None:
    try:
        note_path = VAULT_ROOT / f"{date.today().isoformat()}.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        existing = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
        sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
        block = f"{sep}{_SUMMARY_HEADING}\n\n{brief}\n"
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(block if existing else f"{_SUMMARY_HEADING}\n\n{brief}\n")
        _log.info("[session_brief_worker] wrote brief to %s", note_path)
    except Exception as exc:
        _log.warning("[session_brief_worker] vault write failed: %s", exc)


def main() -> None:
    if len(sys.argv) < 2:
        _log.warning("[session_brief_worker] no events file path given")
        return

    events_path = Path(sys.argv[1])
    try:
        payload = json.loads(events_path.read_text(encoding="utf-8"))
        events = payload.get("events", [])
    except Exception as exc:
        _log.warning("[session_brief_worker] failed to read events file %s: %s", events_path, exc)
        return
    finally:
        try:
            events_path.unlink(missing_ok=True)
        except Exception:
            pass

    if not events:
        return

    brief = _summarize(events)
    if not brief:
        return

    _append_to_daily_note(brief)


if __name__ == "__main__":
    main()
