"""Shadow run of find_cli on whole user prompts (task:444cc036).

The UserPromptSubmit hook runs find_cli's ranking on each prompt and only logs
the result; nothing is shown to the agent. The next `mac <area> <verb>` command
the same session runs is stored beside it, so every row is a free label: the
command the agent actually ran against the one find_cli would have suggested.
That data is what the suggestion gate is tuned on, because the 77-row fixture
(cli-graph tests/eval_prompts.json) cannot show a 1-in-20 wrong rate.

    record(session_id, prompt)       on UserPromptSubmit
    link_calls(session_id, call_ids) on a PostToolUse that ran mac commands
    stats(days=None)                 the report

A mac call links to the session's newest unlinked prompt from the last WINDOW_S
seconds. A prompt no mac call follows keeps chosen NULL: the agent ran no
command, which is the expected answer to a prompt that needs none, or one the
agent served another way. Test sessions are skipped. Prompts are personal, so
the table stays in the local tool_hints.sqlite and nothing is exported without
scrubbing. Entry points never raise.

    uv run python -m src.tools.prompt_shadow [--days N]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from src.db.schema import PROMPT_SHADOW_DDL

WINDOW_S = 15 * 60
MAX_PROMPT = 1000
TEST_PREFIXES = ("test-", "pytest-", "api-test-")

_FINDER = None


def default_db() -> Path:
    from src.config import config
    return config.tool_hints_db


def connect(db: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db or default_db()), timeout=5)
    conn.executescript(PROMPT_SHADOW_DDL)
    return conn


def finder():
    """find_cli's Finder, the class the MCP server uses, loaded once from cli-graph's checkout."""
    global _FINDER
    if _FINDER is None:
        from hooks.paths import CLI_GRAPH_PATH
        root = str(Path(CLI_GRAPH_PATH).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from find_cli.service import Finder
        _FINDER = Finder()
    return _FINDER


def _skip(session_id: str) -> bool:
    return not session_id or session_id.startswith(TEST_PREFIXES)


def record(session_id: str, prompt: str, db=None, now=None, answer=None) -> int | None:
    """Store find_cli's answer for one prompt. Returns the row id, or None if skipped or failed."""
    if _skip(session_id) or not (prompt or "").strip():
        return None
    try:
        res = (answer or finder().answer)(prompt)
        if "error" in res:
            return None
        seeds = res.get("seeds") or []
        conf = res.get("confidence") or {}
        conn = connect(db)
        try:
            cur = conn.execute(
                "INSERT INTO prompt_shadow (ts, session_id, prompt, top, seeds, level, coverage, gap, score) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now or time.time(), session_id, prompt[:MAX_PROMPT],
                 seeds[0]["id"] if seeds else None, json.dumps([s["id"] for s in seeds]),
                 conf.get("level"), conf.get("coverage"), conf.get("gap"),
                 seeds[0].get("score") if seeds else None))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception:
        return None


def link_calls(session_id: str, call_ids: list[str], db=None, now=None) -> str | None:
    """Store the first mac command run after a prompt. Returns it, or None."""
    call_ids = [c for c in call_ids if not c.startswith("mac cli ")]   # a second lookup, not a choice
    if _skip(session_id) or not call_ids:
        return None
    try:
        now = now or time.time()
        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT id FROM prompt_shadow WHERE session_id = ? AND chosen IS NULL AND ts >= ? "
                "ORDER BY ts DESC, id DESC LIMIT 1", (session_id, now - WINDOW_S)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE prompt_shadow SET chosen = ?, linked_ts = ? WHERE id = ?",
                         (call_ids[0], now, row[0]))
            conn.commit()
            return call_ids[0]
        finally:
            conn.close()
    except Exception:
        return None


def stats(days: float | None = None, db=None, now=None) -> dict:
    """Counts by find_cli confidence level: prompts, how many ran a command, and how many
    ran find_cli's top seed (a hit) or another command (a miss)."""
    cutoff = (now or time.time()) - days * 86400 if days else 0
    conn = connect(db)
    try:
        rows = conn.execute("SELECT level, top, chosen FROM prompt_shadow WHERE ts >= ?", (cutoff,)).fetchall()
    finally:
        conn.close()
    out = {"prompts": len(rows), "by_level": {}}
    for level, top, chosen in rows:
        b = out["by_level"].setdefault(level or "none", {"prompts": 0, "ran": 0, "hit": 0, "miss": 0})
        b["prompts"] += 1
        if chosen:
            b["ran"] += 1
            b["hit" if chosen == top else "miss"] += 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="find_cli shadow run on whole prompts")
    ap.add_argument("--days", type=float, default=None)
    print(json.dumps(stats(ap.parse_args(argv).days), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
