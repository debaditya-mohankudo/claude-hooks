"""Grade the model router over time.

Usage: python scripts/route_report.py [--days N] [--db PATH]

Per recommended tier: turns, correction rate (next prompt read as "that was
wrong"), mean output tokens and tool calls, and how often the model actually
used was costlier / cheaper than recommended. Read it as: if `small` shows a
correction rate near `large`'s, the small threshold is too eager; if `large`
turns mostly show few tools and low tokens, it is too cautious.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import config  # noqa: E402

_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}


def _rank(model: str | None) -> int | None:
    m = (model or "").lower()
    return next((v for k, v in _RANK.items() if k in m), None)


def summarize(db: Path | None = None, days: int | None = None) -> list[dict]:
    conn = sqlite3.connect(db or config.log_db)
    conn.row_factory = sqlite3.Row
    where, args = ("WHERE ts >= datetime('now', ?)", (f"-{days} days",)) if days else ("", ())
    try:
        rows = conn.execute(f"SELECT * FROM route_decisions {where}", args).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for tier in ("small", "medium", "large"):
        rs = [r for r in rows if r["tier"] == tier]
        if not rs:
            continue
        done = [r for r in rs if r["actual_model"]]
        rec = {"small": 0, "medium": 1, "large": 2}[tier]
        ranks = [_rank(r["actual_model"]) for r in done]
        ranks = [x for x in ranks if x is not None]
        out.append({
            "tier": tier, "turns": len(rs), "with_outcome": len(done),
            "correction_rate": sum(r["correction"] for r in rs) / len(rs),
            "avg_output_tokens": (sum(r["output_tokens"] or 0 for r in done) / len(done)) if done else None,
            "avg_tool_calls": (sum(r["tool_calls"] or 0 for r in done) / len(done)) if done else None,
            "ran_costlier": sum(x > rec for x in ranks) / len(ranks) if ranks else None,
            "ran_cheaper": sum(x < rec for x in ranks) / len(ranks) if ranks else None,
        })
    return out


def _fmt(v, pct=False):
    if v is None:
        return "-"
    return f"{v:.0%}" if pct else f"{v:.0f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int)
    ap.add_argument("--db", type=Path)
    a = ap.parse_args()
    rows = summarize(a.db, a.days)
    if not rows:
        print("no route_decisions yet")
        return
    print(f"{'tier':8} {'turns':>6} {'outcome':>8} {'correct%':>9} {'avg_tok':>8} {'avg_tools':>9} {'ran>rec':>8} {'ran<rec':>8}")
    for r in rows:
        print(f"{r['tier']:8} {r['turns']:>6} {r['with_outcome']:>8} {_fmt(r['correction_rate'], True):>9} "
              f"{_fmt(r['avg_output_tokens']):>8} {_fmt(r['avg_tool_calls']):>9} "
              f"{_fmt(r['ran_costlier'], True):>8} {_fmt(r['ran_cheaper'], True):>8}")


if __name__ == "__main__":
    main()
