"""Live session dashboard for a kitty split: recent hook-server events.

Read-only view of ~/.claude/server_memory.sqlite (stdlib only). Shows the latest
task activation and the last N prompt / tool events, refreshed every few
seconds. Launch it with `kitten @ launch --location=vsplit python3 kitty/dashboard.py`.

`--view turn` shows the current prompt and final summary; `--view next` shows the
pending plan items. kitty/layout.sh opens them as one tab with a vsplit.

Claude can write a plan / waiting-on-you note to ~/.claude/dashboard_note.md
(CLAUDE_DASHBOARD_NOTE overrides); it is shown above the feed.

Fails soft: a missing or locked DB prints one line and keeps polling; Esc, Ctrl-C
and --once exit 0.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import sqlite3
import sys
import termios
import textwrap
import time
import tty
from datetime import datetime
from pathlib import Path

DB = Path(os.environ.get("CLAUDE_SERVER_MEMORY_DB", "~/.claude/server_memory.sqlite")).expanduser()
NOTE = Path(os.environ.get("CLAUDE_DASHBOARD_NOTE", "~/.claude/dashboard_note.md")).expanduser()
PLAN = Path(os.environ.get("CLAUDE_DASHBOARD_PLAN", "~/.claude/dashboard_plan.json")).expanduser()
TURN = Path(os.environ.get("CLAUDE_DASHBOARD_TURN", "~/.claude/dashboard_turn.json")).expanduser()
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


def _wrap(label: str, text: str, width: int, max_lines: int) -> list[str]:
    body = textwrap.wrap(" ".join((text or "").split()) or "(none yet)", max(width - 2, 10))
    if len(body) > max_lines:
        body = body[:max_lines - 1] + [_clip(body[max_lines - 1], width - 3) + "…"]
    return [label] + [f"  {l}" for l in body]


def read_turn(path: Path, width: int, height: int = 40) -> str:
    """Prompt + final summary of the current turn (hooks/dashboard_turn.py)."""
    try:
        turn = json.loads(path.read_text())
    except (OSError, ValueError):
        return "no turn recorded yet"
    room = max(height - 4, 6)
    out = _wrap("❯ PROMPT", turn.get("prompt", ""), width, max(room // 3, 3))
    out += ["─" * min(width, 60)]
    out += _wrap("★ SUMMARY", turn.get("summary", ""), width, room - len(out))
    return "\n".join(out)


def read_next(path: Path, width: int) -> str:
    """Pending plan items only: the next nodes still to do."""
    try:
        items = json.loads(path.read_text()).get("items", [])
    except (OSError, ValueError):
        return "no plan"
    todo = [i for i in items if not i.get("done")]
    if not todo:
        return "NEXT  nothing pending"
    return "\n".join([f"NEXT {len(todo)} pending"] + [_clip(f"  → {i.get('text', '')}", width) for i in todo])


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


def snapshot(db: Path, n: int, width: int, view: str = "all") -> str:
    if view == "turn":
        return read_turn(TURN, width, os.get_terminal_size().lines if sys.stdout.isatty() else 40)
    if view == "next":
        return read_next(PLAN, width)
    try:
        return render(*fetch(db, n), width, read_plan(PLAN, width) + read_note(NOTE, width))
    except sqlite3.Error as e:
        return f"dashboard: cannot read {db}: {e}"


def _esc_pressed(wait: float) -> bool:
    """Wait up to `wait` seconds; True if a bare Esc arrived (arrow-key sequences don't count)."""
    if not select.select([sys.stdin], [], [], wait)[0]:
        return False
    data = os.read(sys.stdin.fileno(), 32)
    return data == b"\x1b"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-n", type=int, default=15, help="events to show")
    ap.add_argument("-i", "--interval", type=float, default=2.0)
    ap.add_argument("--view", choices=("all", "turn", "next"), default="all",
                    help="all: event feed; turn: prompt + final summary; next: pending plan items")
    ap.add_argument("--once", action="store_true", help="print once and exit")
    args = ap.parse_args(argv)
    fd = sys.stdin.fileno() if sys.stdin.isatty() and not args.once else None
    saved = termios.tcgetattr(fd) if fd is not None else None
    try:
        if fd is not None:
            tty.setcbreak(fd)
        while True:
            width = os.get_terminal_size().columns if sys.stdout.isatty() else 80
            text = snapshot(DB, args.n, width, args.view)
            if args.once:
                print(text)
                return 0
            sys.stdout.write("\x1b[H\x1b[2J" + text + "\n")
            sys.stdout.flush()
            if fd is None:
                time.sleep(args.interval)
            elif _esc_pressed(args.interval):
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    sys.exit(main())
