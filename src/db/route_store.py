"""Persistence for router decisions and their observed outcomes.

One row per prompt. Written at UserPromptSubmit (the recommendation), updated at
Stop (what actually ran: model, tokens, tool calls) and at the *next* prompt
(whether the user's reply reads as a correction). Every function fails open —
a locked or missing DB must never block a turn — and returns a harmless value.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from src.config import config
from src.logger import get_logger

_log = get_logger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS route_decisions (
    prompt_id       TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    ts              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    turn            INTEGER,
    prompt_words    INTEGER,
    tier            TEXT NOT NULL,
    rec_model       TEXT NOT NULL,
    rec_effort      TEXT NOT NULL,
    score           INTEGER NOT NULL,
    reasons         TEXT,
    actual_model    TEXT,
    effort          TEXT,
    output_tokens   INTEGER,
    tool_calls      INTEGER,
    correction      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_route_session_ts ON route_decisions(session_id, ts);
"""

# A reply that opens like this is read as "the last answer was not right".
_CORRECTION = re.compile(
    r"^\s*(no\b|nope|wrong|incorrect|that'?s (not|wrong)|still (broken|failing|not)|"
    r"try again|you (missed|forgot|didn'?t)|doesn'?t work|didn'?t work|not what i)", re.I)


def is_correction(prompt: str) -> bool:
    return bool(_CORRECTION.match(prompt or ""))


def _conn(db: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(db or config.log_db, timeout=2)
    conn.executescript(DDL)
    return conn


def record_decision(*, prompt_id: str, session_id: str, turn: int, prompt: str,
                    tier: str, model: str, effort: str, score: int,
                    reasons: list[str], db: Path | None = None) -> bool:
    try:
        with _conn(db) as c:
            c.execute(
                "INSERT OR REPLACE INTO route_decisions (prompt_id, session_id, turn, prompt_words,"
                " tier, rec_model, rec_effort, score, reasons) VALUES (?,?,?,?,?,?,?,?,?)",
                (prompt_id, session_id, turn, len((prompt or "").split()), tier, model, effort,
                 score, json.dumps(reasons)))
        return True
    except Exception as exc:
        _log.warning("route_store.record_decision failed: %s", exc)
        return False


def mark_correction(session_id: str, db: Path | None = None) -> bool:
    """Flag the session's most recent decision as followed by a correction."""
    try:
        with _conn(db) as c:
            cur = c.execute(
                "UPDATE route_decisions SET correction=1 WHERE prompt_id = ("
                " SELECT prompt_id FROM route_decisions WHERE session_id=? ORDER BY ts DESC, rowid DESC LIMIT 1)",
                (session_id,))
            return cur.rowcount > 0
    except Exception as exc:
        _log.warning("route_store.mark_correction failed: %s", exc)
        return False


def record_outcome(session_id: str, *, actual_model: str | None, effort: str | None,
                   output_tokens: int, tool_calls: int, db: Path | None = None) -> bool:
    """Attach observed outcome to the session's latest decision (idempotent per Stop)."""
    try:
        with _conn(db) as c:
            cur = c.execute(
                "UPDATE route_decisions SET actual_model=?, effort=?, output_tokens=?, tool_calls=?"
                " WHERE prompt_id = (SELECT prompt_id FROM route_decisions WHERE session_id=?"
                " ORDER BY ts DESC, rowid DESC LIMIT 1)",
                (actual_model, effort, output_tokens, tool_calls, session_id))
            return cur.rowcount > 0
    except Exception as exc:
        _log.warning("route_store.record_outcome failed: %s", exc)
        return False


def read_turn_outcome(transcript_path: str, max_bytes: int = 1_000_000) -> dict | None:
    """Model, output tokens and tool-call count for the latest turn in a transcript.

    Walks the tail backwards to the last real user message (string content, or
    blocks that aren't tool_results) and aggregates the assistant entries after it.
    Returns None if the transcript is unreadable or has no assistant entries.
    """
    p = Path(transcript_path or "")
    if not transcript_path or not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return None

    model, tools, seen = None, 0, False
    tok_by_id: dict[str, int] = {}  # one API message is logged once per content block, usage repeated
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("message") or {}
        role = msg.get("role") or e.get("type")
        content = msg.get("content")
        if role == "user":
            blocks = content if isinstance(content, list) else []
            if isinstance(content, str) or any(b.get("type") != "tool_result" for b in blocks if isinstance(b, dict)):
                break
            continue
        if role == "assistant":
            seen = True
            model = model or msg.get("model")
            mid = msg.get("id") or f"anon-{len(tok_by_id)}"
            tok_by_id[mid] = max(tok_by_id.get(mid, 0), int((msg.get("usage") or {}).get("output_tokens") or 0))
            if isinstance(content, list):
                tools += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
    return {"model": model, "output_tokens": sum(tok_by_id.values()), "tool_calls": tools} if seen else None
