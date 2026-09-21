"""Time-split hold-out for often_follows suggestions (task:f1fb2187).

Prompt-free companion to measure_group_routing.py. A first, in-sample replay of
follow suggestions there (edges mined from the sessions it scored, ~49 turns) looked
mildly useful; this one mines edges only from PTU-log transitions BEFORE a cutoff and
scores transitions AFTER it, so it is not in-sample and has hundreds of samples.
The follow_groups implementation it tested was removed (see the tombstone in
langchain_learning/tool_groups.py); the `follow` predictor here is self-contained.
Result at removal: follow beat popularity overall but tied it outside taskfw.

A sample is a group->group transition (same session, group changes, both groups in
the graph -- the exact rule mine_tool_sequences.py uses for edges) whose target is an
MCP group. Predictors, each given the prior group and asked for top-k:

  follow  most frequent MCP successors of the prior group in the training window
  pop     most used MCP groups overall in the training window, prior group excluded
          (the baseline: if follow does not beat this, the "sequence" is popularity)

Usage: python3 scripts/measure_follow_holdout.py [--k 2] [--splits 0.5,0.6,0.7,0.8]
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mine_tool_sequences import HOOKS_DB, PTU, split  # noqa: E402
from langchain_learning.tool_groups import load_graph  # noqa: E402

BUILTIN_PREFIX = "group:claude-code:"


def transitions(group_ids: set[str]) -> list[tuple[str, str]]:
    """Chronological (prior group, next group) pairs, one per group change within a session."""
    con = sqlite3.connect(f"file:{HOOKS_DB}?mode=ro", uri=True)
    last: dict[str, str] = {}
    out = []
    for (msg,) in con.execute("SELECT message FROM hook_logs WHERE message LIKE 'PTU enter:%' ORDER BY id"):
        m = PTU.match(msg)
        if not m:
            continue
        server, grp, _ = split(m[2])
        g = f"group:{server}:{grp}"    # same id mine_tool_sequences.gid builds
        if g not in group_ids:
            continue
        prev = last.get(m[1])
        if prev is not None and prev != g:
            out.append((prev, g))
        last[m[1]] = g
    con.close()
    return out


def evaluate(trans: list[tuple[str, str]], frac: float, k: int) -> dict:
    cut = int(len(trans) * frac)
    train, test = trans[:cut], trans[cut:]
    follow = collections.defaultdict(collections.Counter)
    pop = collections.Counter()
    for x, y in train:
        if not y.startswith(BUILTIN_PREFIX):
            follow[x][y] += 1
            pop[y] += 1
    tests = [(x, y) for x, y in test if not y.startswith(BUILTIN_PREFIX)]
    res = dict(frac=frac, train=len(train), n=len(tests), follow_covered=0, follow=0, pop=0, follow_1=0, pop_1=0)
    for x, y in tests:
        f = [g for g, _ in sorted(follow[x].items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
        p = [g for g, _ in sorted(((g, c) for g, c in pop.items() if g != x), key=lambda kv: (-kv[1], kv[0]))[:k]]
        res["follow_covered"] += bool(f)
        res["follow"] += y in f
        res["pop"] += y in p
        res["follow_1"] += y in f[:1]
        res["pop_1"] += y in p[:1]
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--splits", default="0.5,0.6,0.7,0.8")
    args = ap.parse_args()
    graph = load_graph()
    if not graph or not Path(HOOKS_DB).exists():
        print("missing graph or claude_hooks.sqlite", file=sys.stderr)
        return 1
    ids = {n["id"] for n in graph["nodes"] if n["kind"] == "tool_group"}
    trans = transitions(ids)
    print(f"# follow vs popularity hold-out: {len(trans)} group transitions in PTU log, top-{args.k}")
    print(f"{'train%':>6} {'train':>6} {'test':>5} {'follow@1':>9} {f'follow@{args.k}':>9} {'pop@1':>7} {f'pop@{args.k}':>7} {'covered':>8}")
    for frac in (float(s) for s in args.splits.split(",")):
        r = evaluate(trans, frac, args.k)
        n = r["n"] or 1
        print(f"{frac * 100:>5.0f}% {r['train']:>6} {r['n']:>5} {100 * r['follow_1'] / n:>8.0f}% "
              f"{100 * r['follow'] / n:>8.0f}% {100 * r['pop_1'] / n:>6.0f}% {100 * r['pop'] / n:>6.0f}% "
              f"{100 * r['follow_covered'] / n:>7.0f}%")
    print("covered = test transitions whose prior group had any MCP successor in training")
    print("caveat: one ~5 day window; splits overlap, so rows are not independent samples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
