"""Live session dashboard for a kitty split: recent hook-server events.

Read-only view of ~/.claude/server_memory.sqlite (stdlib only). Shows the latest
task activation and the last N prompt / tool events, refreshed every few
seconds. Launch it with `kitten @ launch --location=vsplit python3 kitty/dashboard.py`.

Claude can write a plan / waiting-on-you note to ~/.claude/dashboard_note.md
(CLAUDE_DASHBOARD_NOTE overrides); it is shown above the feed.

Fails soft: a missing or locked DB prints one line and keeps polling; Ctrl-C and
--once exit 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DB = Path(os.environ.get("CLAUDE_SERVER_MEMORY_DB", "~/.claude/server_memory.sqlite")).expanduser()
NOTE = Path(os.environ.get("CLAUDE_DASHBOARD_NOTE", "~/.claude/dashboard_note.md")).expanduser()
PLAN = Path(os.environ.get("CLAUDE_DASHBOARD_PLAN", "~/.claude/dashboard_plan.json")).expanduser()
_SHOWN = ("prompt", "tool", "task")
_GLYPH = {"prompt": "❯", "tool": "·", "task": "★"}


def _clip(text: str | None, width: int) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= width else one[: max(width - 1, 0)] + "…"


def fetch(db: Path, n: int) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    """(latest task activation, last n shown events oldest-first)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
    con.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" * len(_SHOWN))
        task = con.execute(
            "SELECT ts, content, ref FROM server_memory WHERE type='task' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        rows = con.execute(
            f"SELECT ts, type, content, ref FROM server_memory WHERE type IN ({marks}) "
            "ORDER BY ts DESC LIMIT ?", (*_SHOWN, n),
        ).fetchall()
    finally:
        con.close()
    return task, rows[::-1]


def read_note(path: Path, width: int, max_lines: int = 12) -> list[str]:
    """Claude-written status (plan / waiting-on-you), shown above the event feed."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    lines = [_clip(l, width) for l in lines if l.strip()][:max_lines]
    return lines + ["─" * min(width, 60)] if lines else []


def read_plan(path: Path, width: int, max_lines: int = 12) -> list[str]:
    """Hook-mirrored plan (hooks/dashboard_plan.py): title, then [x]/[ ] items."""
    try:
        plan = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    items = plan.get("items", [])
    if not items and not plan.get("title"):
        return []
    done = sum(1 for i in items if i.get("done"))
    head = f"PLAN {done}/{len(items)}" + (f"  {plan['title']}" if plan.get("title") else "")
    lines = [_clip(head, width)]
    lines += [_clip(f"  [{'x' if i.get('done') else ' '}] {i.get('text', '')}", width) for i in items]
    return lines[:max_lines] + ["─" * min(width, 60)]


def render(task, rows, width: int, note: list[str] | None = None) -> str:
    out = list(note or [])
    if task:
        title = _clip(task["content"], width - 14) or "(no title logged)"
        out.append(f"★ {title}  [{task['ref'] or '?'}]")
    else:
        out.append("★ no task activation in the event window")
    out.append("─" * min(width, 60))
    for r in rows:
        t = datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
        out.append(f"{t} {_GLYPH.get(r['type'], '?')} {_clip(r['content'], width - 11)}")
    return "\n".join(out)


def snapshot(db: Path, n: int, width: int) -> str:
    try:
        return render(*fetch(db, n), width, read_plan(PLAN, width) + read_note(NOTE, width))
    except sqlite3.Error as e:
        return f"dashboard: cannot read {db}: {e}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-n", type=int, default=15, help="events to show")
    ap.add_argument("-i", "--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true", help="print once and exit")
    args = ap.parse_args(argv)
    try:
        while True:
            width = os.get_terminal_size().columns if sys.stdout.isatty() else 80
            text = snapshot(DB, args.n, width)
            if args.once:
                print(text)
                return 0
            sys.stdout.write("\x1b[H\x1b[2J" + text + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
