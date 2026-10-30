"""find_cli hit rate from real use (task:cf5d016d).

Each find_cli lookup is stored with the commands it suggested, then linked to
the next `mac <area> <verb>` command the same session runs:

    record_query(session_id, tool_input, tool_response)  on mcp__find_cli__find_cli
    link_calls(session_id, call_ids)                      on any mac call
    label(row_id, "no_fit")                               hand label (task:9d42faae)
    stats(days=None)                                      the hit-rate report

A mac call links to that session's newest unlinked query from the last
WINDOW_S seconds. When one Bash line runs several mac calls, the chosen one is
the first that was a seed, else the first neighbour, else the first call (a
miss). A query no mac call follows keeps outcome NULL and counts as unused,
not as a miss. Test sessions are skipped. The hook entry points never raise.

A query no command covers (e.g. a data series no verb fetches) is hand-labelled
no_fit after review. It replaces a NULL or miss outcome, never a seed or
neighbour, and is counted apart from the hit rates, so a correct "nothing
fits" is not a miss.

Table find_cli_queries (src/db/schema.py) lives in ~/.claude/tool_hints.sqlite
beside the per-command counts. Report and label:

    uv run python -m src.tools.find_cli_log [--days N] [--misses N]
    uv run python -m src.tools.find_cli_log --label ROW_ID no_fit

Eval cases (task:71dc0383): `--export` prints, as JSON on stdout, the linked
or no_fit queries whose text is in none of the `--exclude` fixture files, in
the held-out row shape plus id/outcome/chosen. A human reviews them (scrub
personal data, fix `expect`) before copying any into the held-out fixture.

    uv run python -m src.tools.find_cli_log --export \
        --exclude ../cli-graph/tests/eval_phrases.json --exclude ../cli-graph/tests/eval_heldout.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from src.db.schema import FIND_CLI_QUERIES_DDL

FIND_TOOL = "mcp__find_cli__find_cli"
WINDOW_S = 15 * 60
TEST_PREFIXES = ("test-", "pytest-", "api-test-")
LABELS = ("no_fit",)
LABELLABLE = ("miss", None)   # outcomes a hand label may replace


def default_db() -> Path:
    from src.config import config
    return config.tool_hints_db


def connect(db: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db or default_db()), timeout=5)
    conn.executescript(FIND_CLI_QUERIES_DDL)
    return conn


def parse_result(tool_response) -> dict | None:
    """find_cli's {query, seeds, neighbours} from any shape the hook receives it in."""
    resp = tool_response
    if isinstance(resp, dict) and "seeds" in resp:
        return resp
    if isinstance(resp, dict):
        resp = resp.get("content") if "content" in resp else resp.get("raw")
    if isinstance(resp, list):
        resp = next((b.get("text") for b in resp
                     if isinstance(b, dict) and b.get("type") == "text"), None)
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except ValueError:
            return None
    return resp if isinstance(resp, dict) and "seeds" in resp else None


def _ids(rows) -> list[str]:
    return [r["id"] for r in rows or () if isinstance(r, dict) and "id" in r]


def _skip(session_id: str) -> bool:
    return not session_id or session_id.startswith(TEST_PREFIXES)


def record_query(session_id: str, tool_input: dict, tool_response, db=None, now=None) -> int | None:
    """Store one find_cli lookup. Returns its row id, or None if skipped or failed."""
    if _skip(session_id):
        return None
    try:
        result = parse_result(tool_response)
        if result is None:
            return None
        query = result.get("query") or (tool_input or {}).get("task") or ""
        conn = connect(db)
        try:
            cur = conn.execute(
                "INSERT INTO find_cli_queries (ts, session_id, query, seeds, neighbours) "
                "VALUES (?, ?, ?, ?, ?)",
                (now or time.time(), session_id, query,
                 json.dumps(_ids(result.get("seeds"))), json.dumps(_ids(result.get("neighbours")))))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception:
        return None


def judge(call_ids: list[str], seeds: list[str], neighbours: list[str]) -> tuple[str, int | None, str]:
    """(chosen, 1-based seed rank or None, outcome) for the mac calls one tool call ran."""
    for cid in call_ids:
        if cid in seeds:
            return cid, seeds.index(cid) + 1, "seed"
    for cid in call_ids:
        if cid in neighbours:
            return cid, None, "neighbour"
    return call_ids[0], None, "miss"


def link_calls(session_id: str, call_ids: list[str], db=None, now=None) -> str | None:
    """Link mac calls to the session's pending find_cli query. Returns the outcome, or None."""
    # `mac cli find` is the CLI fallback for find_cli itself (task:ddc32293): running it
    # after a query is a second lookup, not a chosen command, so it must not be judged a miss.
    call_ids = [c for c in call_ids if not c.startswith("mac cli ")]
    if _skip(session_id) or not call_ids:
        return None
    try:
        now = now or time.time()
        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT id, seeds, neighbours FROM find_cli_queries "
                "WHERE session_id = ? AND outcome IS NULL AND ts >= ? ORDER BY ts DESC, id DESC LIMIT 1",
                (session_id, now - WINDOW_S)).fetchone()
            if row is None:
                return None
            chosen, rank, outcome = judge(list(call_ids), json.loads(row[1]), json.loads(row[2]))
            conn.execute(
                "UPDATE find_cli_queries SET chosen = ?, chosen_rank = ?, outcome = ?, linked_ts = ? "
                "WHERE id = ?", (chosen, rank, outcome, now, row[0]))
            conn.commit()
            return outcome
        finally:
            conn.close()
    except Exception:
        return None


def label(row_id: int, outcome: str, db=None, now=None) -> str:
    """Hand-label one query. Returns the outcome it replaced (None for an unused row).

    Raises ValueError for an unknown label, a missing row, or a row whose
    outcome is seed/neighbour (the agent ran a suggested command, so it fit).
    """
    if outcome not in LABELS:
        raise ValueError(f"unknown label {outcome!r}; expected one of {', '.join(LABELS)}")
    conn = connect(db)
    try:
        row = conn.execute("SELECT outcome FROM find_cli_queries WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise ValueError(f"no find_cli query with id {row_id}")
        if row[0] == outcome:
            return row[0]
        if row[0] not in LABELLABLE:
            raise ValueError(f"query {row_id} is a {row[0]}: a suggested command was run, so it cannot be {outcome}")
        conn.execute("UPDATE find_cli_queries SET outcome = ?, linked_ts = ? WHERE id = ?",
                     (outcome, now or time.time(), row_id))
        conn.commit()
        return row[0]
    finally:
        conn.close()


def stats(db=None, days: float | None = None, misses: int = 10, now=None) -> dict:
    """Hit rates over linked queries, plus the most recent misses. no_fit is not linked."""
    conn = connect(db)
    try:
        since = (now or time.time()) - days * 86400 if days else 0
        counts = dict(conn.execute(
            "SELECT COALESCE(outcome, 'unused'), COUNT(*) FROM find_cli_queries "
            "WHERE ts >= ? GROUP BY 1", (since,)).fetchall())
        top1 = conn.execute(
            "SELECT COUNT(*) FROM find_cli_queries WHERE ts >= ? AND chosen_rank = 1",
            (since,)).fetchone()[0]
        recent = conn.execute(
            "SELECT datetime(ts, 'unixepoch', 'localtime'), query, chosen, seeds "
            "FROM find_cli_queries WHERE ts >= ? AND outcome = 'miss' ORDER BY ts DESC LIMIT ?",
            (since, misses)).fetchall()
    finally:
        conn.close()
    seed, nbr, miss = (counts.get(k, 0) for k in ("seed", "neighbour", "miss"))
    linked = seed + nbr + miss

    def rate(n):
        return round(n / linked, 3) if linked else None

    no_fit = counts.get("no_fit", 0)
    return {
        "queries": linked + counts.get("unused", 0) + no_fit,
        "linked": linked,
        "unused": counts.get("unused", 0),
        "no_fit": no_fit,
        "seed": seed, "neighbour": nbr, "miss": miss,
        "top1_rate": rate(top1),
        "seed_rate": rate(seed),
        "subgraph_rate": rate(seed + nbr),
        "recent_misses": [{"ts": ts, "query": q, "chosen": c, "seeds": json.loads(s)}
                          for ts, q, c, s in recent],
    }


def _norm(text: str) -> str:
    return text.strip().lower()


def export_candidates(excludes=(), db=None) -> list[dict]:
    """Logged queries not yet in any fixture in `excludes`, as held-out rows for hand review.

    Matching is on lowercased, stripped query text, as the eval's own overlap
    test does. Outcome maps to a draft `expect`: seed/neighbour/miss -> the
    command the agent ran (a miss needs the most review); no_fit -> [];
    unused (NULL) -> [] flagged, since no command ran to say what fit. Repeats
    of one query text collapse to the newest row.
    """
    known = set()
    for path in excludes:
        with open(path) as f:
            known.update(_norm(p["phrase"]) for p in json.load(f)["phrases"])
    conn = connect(db)
    try:
        found = conn.execute(
            "SELECT id, query, chosen, outcome FROM find_cli_queries ORDER BY ts, id").fetchall()
    finally:
        conn.close()
    newest = {}
    for rid, query, chosen, outcome in found:
        if query and _norm(query) not in known:
            newest[_norm(query)] = (rid, query, chosen, outcome)
    return [{"phrase": q, "expect": [c] if c and o != "no_fit" else [], "source": "logged",
             "id": rid, "outcome": o or "unused", "chosen": c}
            for rid, q, c, o in newest.values()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="find_cli hit rate from real use")
    ap.add_argument("--days", type=float, help="only queries from the last N days")
    ap.add_argument("--misses", type=int, default=10, help="recent misses to list")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--label", nargs=2, metavar=("ROW_ID", "OUTCOME"),
                    help="hand-label one query, e.g. --label 5 no_fit")
    ap.add_argument("--export", action="store_true",
                    help="print logged queries not in any --exclude fixture, for hand review")
    ap.add_argument("--exclude", action="append", default=[], metavar="FIXTURE",
                    help="fixture JSON whose phrases --export leaves out (repeatable)")
    args = ap.parse_args(argv)
    if args.export:
        print(json.dumps({"phrases": export_candidates(args.exclude)}, indent=2))
        return 0
    if args.label:
        try:
            was = label(int(args.label[0]), args.label[1])
        except ValueError as e:
            print(f"error: {e}")
            return 1
        print(f"query {args.label[0]}: {was or 'unused'} -> {args.label[1]}")
        return 0
    s = stats(days=args.days, misses=args.misses)
    if args.json:
        print(json.dumps(s, indent=2))
        return 0

    def pct(r):
        return "-" if r is None else f"{r:.0%}"

    print(f"queries {s['queries']}  linked {s['linked']}  unused {s['unused']}  no_fit {s['no_fit']}")
    print(f"top-1 {pct(s['top1_rate'])}  seed {pct(s['seed_rate'])}  subgraph {pct(s['subgraph_rate'])}"
          f"  (seed {s['seed']}, neighbour {s['neighbour']}, miss {s['miss']})")
    for m in s["recent_misses"]:
        print(f"  miss {m['ts']}  {m['query']!r} -> ran {m['chosen']}; seeds {', '.join(m['seeds'][:3])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
