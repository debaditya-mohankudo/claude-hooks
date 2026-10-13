"""Compare memory recall top-N: legacy `related` boost vs memory_links boost (task:5530af93).

Replays recent prompts from the hook server's event log against the live MEMORY.sqlite
(read-only) under both related_source settings and prints where the top-N differs.

    uv run python scripts/compare_memory_recall.py [--prompts 30] [--top-n 3] [--show-same]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from langchain_learning.nodes import _memory_scoring as ms  # noqa: E402
from langchain_learning.nodes._text_utils import tokenise  # noqa: E402

MEMORY_DB = Path.home() / ".claude" / "MEMORY.sqlite"
EVENT_DB = Path.home() / ".claude" / "server_memory.sqlite"


def recent_prompts(n: int) -> list[str]:
    with sqlite3.connect(f"file:{EVENT_DB}?mode=ro", uri=True) as con:
        rows = con.execute(
            "SELECT content FROM server_memory WHERE type='prompt' AND length(content) > 15 "
            "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    return [r[0] for r in rows]


def recall(conn, tokens: set[str], source: str, top_n: int) -> list[dict]:
    cfg = {**ms.load_scoring_cfg(), "related_source": source}
    with patch.object(ms, "load_scoring_cfg", return_value=cfg):
        return ms.score_memories(tokens, conn, top_n=top_n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", type=int, default=30)
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--show-same", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    changed = overlap = total = conflicts = 0
    for p in recent_prompts(a.prompts):
        tokens = set(tokenise(p.lower()))
        old = [m["name"] for m in recall(conn, tokens, "related", a.top_n)]
        new_m = recall(conn, tokens, "links", a.top_n)
        new = [m["name"] for m in new_m]
        conflicts += sum(1 for m in new_m if m.get("conflicts_with"))
        total += 1
        overlap += len(set(old) & set(new))
        if old != new:
            changed += 1
        if old != new or a.show_same:
            print(f"\n> {p[:90]!r}\n  related: {old}\n  links  : {new}")
    print(f"\n{total} prompts: top-{a.top_n} changed for {changed}; "
          f"mean overlap {overlap / max(total, 1):.2f}/{a.top_n}; conflict-marked results {conflicts}")
