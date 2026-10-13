"""Propose typed memory edges for pairs that already share a relates_to edge (task:5484cd03).

Default mode is read-only: prints a ranked, reviewable proposal list. Nothing is written
without `--apply approved.json`, a JSON list of {"src": name, "dst": name, "type": type}
taken from that list; each entry must match a current proposal or it is refused.

    uv run python scripts/suggest_memory_edge_types.py [--db PATH] [--json]
    uv run python scripts/suggest_memory_edge_types.py --apply approved.json

Candidate universe is the existing relates_to graph; no new pairs are invented. Three
signals only: (1) a typing phrase within a short window of the other memory's quoted slug,
(2) the slug quoted at all (needed by 1), (3) dated slugs sharing a stem. Precision over
recall: a phrase with no quoted slug nearby proposes nothing.
Direction: supersedes/extends/learned_from are stored src=newer/deriving; contradicts is
stored src<dst (symmetric).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill_memory_links import DEFAULT_DB, snapshot  # noqa: E402

WINDOW = 80  # chars either side of a quoted slug in which a phrase must fall

# (regex, type, phrase_owner_is_src): the memory whose body holds the phrase is the newer
# one (src) for "replaces X"; for "superseded by X" / "obsolete, see X" it is the older.
PHRASES = [
    (re.compile(r"\b(supersedes|replaces)\b", re.I), "supersedes", True),
    (re.compile(r"\b(superseded (?:by|\(\d{4}-\d{2}-\d{2}\))|replaced by|obsoleted by)", re.I), "supersedes", False),
    (re.compile(r"\b(contradicts|contradicted by|corrects|corrected by)\b", re.I), "contradicts", True),
    (re.compile(r"\bextends\b", re.I), "extends", True),
    (re.compile(r"\blearned from\b", re.I), "learned_from", True),
]
DATE = re.compile(r"-?(\d{4}-\d{2}(?:-\d{2})?)")
CONFIDENCE = {"supersedes": 3, "contradicts": 3, "learned_from": 2, "extends": 2, "dated": 1}


def _phrase_hits(body: str, other: str):
    """Yield (type, owner_is_src, quote) where a typing phrase sits near `other`'s slug."""
    for m in re.finditer(re.escape(other), body):
        lo, hi = max(0, m.start() - WINDOW), min(len(body), m.end() + WINDOW)
        window = body[lo:hi]
        hits = [(t, o) for rx, t, o in PHRASES if rx.search(window)]
        if len({(t, o) for t, o in hits if t == "supersedes"}) > 1:
            continue  # "superseded ... replaces" in one window: direction is ambiguous, propose nothing
        for ltype, owner_src in hits:
            yield ltype, owner_src, " ".join(window.split())


def _dated(a: str, b: str):
    """Newer/older by date when two slugs differ only in their date; else None."""
    da, db_ = DATE.search(a), DATE.search(b)
    if not da or not db_ or da.group(1) == db_.group(1):
        return None
    if DATE.sub("", a) != DATE.sub("", b):
        return None
    return (a, b) if da.group(1) > db_.group(1) else (b, a)


def propose(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        """SELECT s.id, s.name, s.body, d.id, d.name, d.body FROM memory_links l
           JOIN memories s ON s.id = l.src_id JOIN memories d ON d.id = l.dst_id
           WHERE l.type = 'relates_to'"""
    ).fetchall()
    bodies = {n: b or "" for _, n, b, _, _, _ in rows} | {n: b or "" for _, _, _, _, n, b in rows}
    out: dict[tuple[str, str, str], dict] = {}
    for _, a, _, _, b, _ in rows:
        for owner, other in ((a, b), (b, a)):
            for ltype, owner_src, quote in _phrase_hits(bodies[owner], other):
                new, old = (owner, other) if owner_src else (other, owner)
                if ltype == "contradicts":
                    new, old = sorted((new, old))
                out.setdefault((new, old, ltype), {
                    "src": new, "dst": old, "type": ltype, "signal": "phrase",
                    "evidence": f'{owner}: "…{quote}…"',
                })
        pair = _dated(a, b)
        if pair:
            out.setdefault((pair[0], pair[1], "extends"), {
                "src": pair[0], "dst": pair[1], "type": "extends", "signal": "dated",
                "evidence": f"dated pair: {pair[0]} is later than {pair[1]}",
            })
    props = list(out.values())
    # An undirected conflict of direction (same pair proposed both ways) is not trustworthy.
    pairs = {}
    for p in props:
        pairs.setdefault(frozenset((p["src"], p["dst"])), []).append(p)
    props = [p for grp in pairs.values() if len({(q["src"], q["type"]) for q in grp}) == 1 for p in grp[:1]]
    props.sort(key=lambda p: (-CONFIDENCE["dated" if p["signal"] == "dated" else p["type"]], p["src"], p["dst"]))
    return props


def stale_references(con: sqlite3.Connection, props: list[dict]) -> list[dict]:
    """For each proposed supersedes, memories that quote the old slug but not the new one."""
    out = []
    for p in props:
        if p["type"] != "supersedes":
            continue
        for (name,) in con.execute(
            "SELECT name FROM memories WHERE body LIKE ? AND body NOT LIKE ? AND name NOT IN (?, ?)",
            (f"%{p['dst']}%", f"%{p['src']}%", p["src"], p["dst"]),
        ):
            out.append({"superseded": p["dst"], "by": p["src"], "still_referenced_by": name})
    return out


def apply(db: Path, approved: list[dict]) -> dict:
    """Retype approved edges. Snapshot first; refuses anything not a current proposal."""
    with sqlite3.connect(db) as con:
        current = {(p["src"], p["dst"], p["type"]) for p in propose(con)}
    bad = [a for a in approved if (a["src"], a["dst"], a["type"]) not in current]
    if bad:
        raise ValueError(f"not a current proposal (already applied or never proposed): {bad}")
    snap = snapshot(db)
    changed = 0
    with sqlite3.connect(db) as con:
        con.execute("PRAGMA foreign_keys = ON")
        for a in approved:
            ids = {n: i for i, n in con.execute(
                "SELECT id, name FROM memories WHERE name IN (?, ?)", (a["src"], a["dst"]))}
            lo, hi = sorted(ids.values())
            if con.execute("DELETE FROM memory_links WHERE type='relates_to' AND src_id=? AND dst_id=?",
                           (lo, hi)).rowcount == 0:
                continue  # no relates_to edge left: rerun, nothing to convert
            sym = con.execute("SELECT symmetric FROM edge_types WHERE type=?", (a["type"],)).fetchone()[0]
            s, d = (lo, hi) if sym else (ids[a["src"]], ids[a["dst"]])
            con.execute("INSERT OR IGNORE INTO memory_links (src_id, dst_id, type) VALUES (?,?,?)",
                        (s, d, a["type"]))
            changed += 1
        counts = dict(con.execute("SELECT type, COUNT(*) FROM memory_links GROUP BY type"))
    return {"snapshot": str(snap), "changed": changed, "counts": counts}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true", help="emit proposals as JSON")
    ap.add_argument("--apply", type=Path, help="JSON file of approved proposals")
    args = ap.parse_args()
    if args.apply:
        print(json.dumps(apply(args.db, json.loads(args.apply.read_text())), indent=2))
        return
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as con:
        props = propose(con)
        stale = stale_references(con, props)
    if args.json:
        print(json.dumps({"proposals": props, "still_referenced": stale}, indent=2))
        return
    print(f"{len(props)} proposals (read-only; nothing written)\n")
    for i, p in enumerate(props, 1):
        print(f"{i:3}. [{p['type']}] {p['src']} -> {p['dst']}  ({p['signal']})\n      {p['evidence']}")
    if stale:
        print("\nSuperseded but still referenced without the superseder:")
        for s in stale:
            print(f"  {s['still_referenced_by']} mentions {s['superseded']} (superseded by {s['by']})")


if __name__ == "__main__":
    main()
