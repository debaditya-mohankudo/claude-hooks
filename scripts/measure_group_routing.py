"""Measure whether graph-keyword group matching routes better than the DB scorer.

For every prompt retained in ~/.claude/server_memory.sqlite (full prompt text; a
rolling window, so the sample differs from week to week -- the window bounds and
size are printed with the result), compare two sources of "suggested groups":

  db    KeywordOverlapScorer hints from ~/.claude/tool_hints.sqlite lifted to groups
        (what '## Suggested tool groups' shows today), top 3 by hint count
  graph exact-token match of the prompt against curated `keywords` on the
        tool_group nodes of ontology/mcp-tools-domain.json, top 3 by matched-keyword count

against the tools the model actually used in that turn (task:21353636 decision:
any MCP tool used between this prompt and the next one in the same Claude session,
restricted to tools present in BOTH tool_hints.sqlite and the graph so the two
scorers are compared like with like).

Read the result with the biases in mind, printed at the end: the DB's keyword
column and counts were learned from these same prompts, so replay favours the DB;
and the window is small.

Usage: python3 scripts/measure_group_routing.py [--top-groups 3] [--examples 5]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_learning.config import config
from langchain_learning.nodes._text_utils import tokenise
from langchain_learning.retrievers import KeywordOverlapScorer
from langchain_learning.tool_groups import _index, group_of, load_graph, match_groups

SERVER_MEMORY_DB = Path.home() / ".claude" / "server_memory.sqlite"
# Append-only copy (task:894f0a65); the live window above is a capped rolling one.
SNAPSHOT_DB = Path.home() / ".claude" / "routing_snapshot.sqlite"


def turns(rows: list[tuple]) -> list[tuple[str, str, list[str]]]:
    """(session, prompt text, [tool short-names used in that turn]) from server_memory
    rows of (claude_session_id, ts, type, content). A turn is a prompt plus every
    'tool' row after it in the same session up to that session's next prompt."""
    by_session: dict[str, list[tuple]] = {}
    for sess, ts, typ, content in rows:
        by_session.setdefault(sess, []).append((ts, typ, content))
    out = []
    for sess, evs in by_session.items():
        cur = None
        for _, typ, content in sorted(evs, key=lambda e: e[0]):
            if typ == "prompt":
                cur = (sess, content or "", [])
                out.append(cur)
            elif typ == "tool" and cur is not None and content:
                cur[2].append(content)
    return out


def rank_graph(matched: dict[str, list[str]], top: int) -> list[str]:
    """Group ids by matched-keyword count, then id (deterministic)."""
    return sorted(matched, key=lambda g: (-len(matched[g]), g))[:top]


def evaluate(turn_list, graph, db_scorer, db_names: set[str], top_groups: int = 3) -> dict:
    groups, _, by_name = _index(graph)
    res = dict(prompts=0, evaluated=0, db_no_hints=0, db_hints_no_group=0,
               graph_only=0, tool_in_db=0, group_db=0, group_graph=0, group_either=0,
               db_groups_shown=0, graph_groups_shown=0, examples=[])
    for _, prompt, used in turn_list:
        res["prompts"] += 1
        kws = tokenise(prompt)
        hints = db_scorer.score(kws, top_n=5)
        db_gids: list[str] = []
        counts: dict[str, int] = {}
        for h in hints:
            g = group_of(h["tool_name"], groups, by_name)
            if g:
                counts[g] = counts.get(g, 0) + 1
        db_gids = sorted(counts, key=lambda g: (-counts[g], g))[:top_groups]
        gr_gids = rank_graph(match_groups(kws, graph), top_groups)

        if not hints:
            res["db_no_hints"] += 1
        elif not counts:
            res["db_hints_no_group"] += 1
        if gr_gids and not hints:
            res["graph_only"] += 1

        eligible = [t for t in used if t in db_names and group_of(t, groups, by_name)]
        if not eligible:
            continue
        res["evaluated"] += 1
        res["db_groups_shown"] += len(db_gids)
        res["graph_groups_shown"] += len(gr_gids)
        hint_names = {h["tool_name"] for h in hints}
        used_groups = {group_of(t, groups, by_name) for t in eligible}
        res["tool_in_db"] += bool(hint_names & set(eligible))
        in_db = bool(used_groups & set(db_gids))
        in_graph = bool(used_groups & set(gr_gids))
        res["group_db"] += in_db
        res["group_graph"] += in_graph
        res["group_either"] += in_db or in_graph
        if len(res["examples"]) < 5 and in_graph != in_db:
            res["examples"].append((prompt[:80].replace("\n", " "), sorted(eligible)[:3],
                                    "graph-only" if in_graph else "db-only"))
    return res


def report(res: dict, window: tuple[float, float] | None) -> str:
    n = res["evaluated"] or 1
    pct = lambda x, d=n: f"{x}/{d} = {100 * x / d:.0f}%" if d else "n/a"
    lines = ["# group-routing measurement",
             f"prompts in window: {res['prompts']}; evaluated (turn used >=1 eligible MCP tool): {res['evaluated']}"]
    if window:
        from datetime import datetime
        f = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
        lines.append(f"window: {f(window[0])} .. {f(window[1])}")
    p = res["prompts"] or 1
    lines += [
        f"DB scorer gave no hints:                {pct(res['db_no_hints'], p)} of all prompts",
        f"DB hints that resolve to no group:      {pct(res['db_hints_no_group'], p)} of all prompts",
        f"graph matched a group, DB had no hints: {pct(res['graph_only'], p)} of all prompts",
        "-- did the thing the model used next appear? (evaluated turns) --",
        f"tool in DB top-5 hints:      {pct(res['tool_in_db'])}",
        f"group in DB-routed groups:   {pct(res['group_db'])}   (avg shown {res['db_groups_shown'] / n:.2f})",
        f"group in graph-keyword top:  {pct(res['group_graph'])}   (avg shown {res['graph_groups_shown'] / n:.2f})",
        f"group in either:             {pct(res['group_either'])}",
        "-- disagreements --"]
    lines += [f"  [{tag}] {p!r} -> {tools}" for p, tools, tag in res["examples"]] or ["  none"]
    lines += ["caveats: DB keywords/counts were learned from these same prompts (replay favours DB);",
              "         sample grows over time; compare runs by the printed window, not by run."]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-groups", type=int, default=3)
    ap.add_argument("--source", choices=("snapshot", "live"), default="snapshot",
                    help="snapshot: append-only routing_snapshot.sqlite (default); live: capped server_memory window")
    args = ap.parse_args()
    graph = load_graph()
    db, table = (SNAPSHOT_DB, "routing_snapshot") if args.source == "snapshot" else (SERVER_MEMORY_DB, "server_memory")
    if not graph or not db.exists() or not config.tool_hints_db.exists():
        print(f"missing graph, {db.name} or tool_hints.sqlite", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(f"SELECT claude_session_id, ts, type, content FROM {table} "
                        "WHERE type IN ('prompt','tool')").fetchall()
    conn.close()
    hconn = sqlite3.connect(f"file:{config.tool_hints_db}?mode=ro", uri=True)
    db_names = {r[0] for r in hconn.execute("SELECT tool_name FROM mcp_tool_hints")}
    hconn.close()
    tl = turns(rows)
    ts = [r[1] for r in rows]
    print(report(evaluate(tl, graph, KeywordOverlapScorer(), db_names, args.top_groups),
                 (min(ts), max(ts)) if ts else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
