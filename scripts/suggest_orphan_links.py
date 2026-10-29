"""Propose relates_to neighbours for orphan memories — those with no memory_links edge (task:d4f9a585).

Default mode is read-only: prints every orphan with up to 3 proposed neighbours and the
evidence for each, or "no confident match". Nothing is written without `--apply approved.json`,
a JSON list taken from that report:

    {"memory": orphan, "neighbour": name}     link them (relates_to)
    {"standalone": name}                      record as reviewed, never re-proposed

    uv run python scripts/suggest_orphan_links.py [--db PATH] [--json]
    uv run python scripts/suggest_orphan_links.py --apply approved.json [--prompts 30] [--top-n 3]

Evidence, strongest first: (1) one memory's slug quoted in the other's body, (2) a shared
files/docs path, (3) shared *rare* tags (document frequency under COMMON_FRAC of all memories;
a tag on hundreds of memories is not evidence). One rare tag alone proposes nothing; two is
"medium", three or any quote/path is "high". Candidates are never self, an existing edge, or a
hub (degree > MAX_DEGREE: one bad link to a hub spreads to every prompt that hits it). Slugs
that name no memory (dangling) cannot be candidates and are never touched; find them with
`backfill_memory_links.py --report <tsv>`.

"Reviewed standalone" lives in memory_link_reviews, created lazily here rather than in
MEMORY_LINKS_DDL: no new memories column, no startup migration, and losing it only means an
orphan is proposed again.

Apply order: validate every entry against the current proposals (any stale/duplicate/unknown
entry refuses the whole file before a byte is written), snapshot (integrity_check), write,
report orphans before/after, then replay recent prompts to show whose top-N moved.
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

MAX_PER_ORPHAN = 3
COMMON_FRAC = 0.05   # a tag on more than this fraction of memories is not evidence
MIN_COMMON_DF = 10   # ...but never call a tag common below this many memories
MAX_DEGREE = 40      # skip neighbours that are already hubs
MIN_SLUG_LEN = 8     # shorter names match prose by accident

REVIEWS_DDL = """
CREATE TABLE IF NOT EXISTS memory_link_reviews (
    memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    reviewed  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)"""


def _tags(s: str | None) -> set[str]:
    return {t.strip().lower() for t in (s or "").split(",") if t.strip()}


def _paths(files: str | None, docs: str | None) -> set[str]:
    """Path-looking entries only: the files column also holds free-text notes in places."""
    out = set()
    for item in f"{files or ''},{docs or ''}".split(","):
        item = item.strip().lower()
        if item and " " not in item and ("/" in item or "." in item):
            out.add(item)
    return out


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def orphan_ids(con: sqlite3.Connection) -> list[int]:
    return [r[0] for r in con.execute(
        "SELECT id FROM memories WHERE id NOT IN (SELECT src_id FROM memory_links) "
        "AND id NOT IN (SELECT dst_id FROM memory_links) ORDER BY id")]


def _reviewed(con: sqlite3.Connection) -> set[int]:
    if not _has_table(con, "memory_link_reviews"):
        return set()
    return {r[0] for r in con.execute("SELECT memory_id FROM memory_link_reviews")}


def propose(con: sqlite3.Connection) -> dict:
    """{"orphans": [{memory, proposals: [...]}], "reviewed": n, "common_tags": [...]}."""
    mems = {i: {"name": n, "body": b or "", "tags": _tags(t), "paths": _paths(f, d)}
            for i, n, b, t, f, d in con.execute("SELECT id, name, body, tags, files, docs FROM memories")}
    total = len(mems)
    df: dict[str, int] = {}
    for m in mems.values():
        for t in m["tags"]:
            df[t] = df.get(t, 0) + 1
    common = {t for t, c in df.items() if c >= MIN_COMMON_DF and c > COMMON_FRAC * total}
    edges: dict[int, set[int]] = {}
    for a, b in con.execute("SELECT src_id, dst_id FROM memory_links"):
        edges.setdefault(a, set()).add(b)
        edges.setdefault(b, set()).add(a)
    reviewed = _reviewed(con)
    orphans = orphan_ids(con)
    result = []
    for oid in orphans:
        if oid in reviewed:
            continue
        o = mems[oid]
        cands = []
        for cid, c in mems.items():
            if cid == oid or cid in edges.get(oid, ()) or len(edges.get(cid, ())) > MAX_DEGREE:
                continue
            evidence, score = [], 0
            for a, b in ((o, c), (c, o)):
                if len(a["name"]) >= MIN_SLUG_LEN and a["name"] in b["body"]:
                    evidence.append(f'slug "{a["name"]}" quoted in {b["name"]}\'s body')
                    score += 3
            shared_paths = sorted(o["paths"] & c["paths"])
            if shared_paths:
                evidence.append("shared path: " + ", ".join(shared_paths[:3]))
                score += 2
            shared = sorted(o["tags"] & c["tags"])
            rare = [t for t in shared if t not in common]
            if len(rare) >= 2 or (rare and score):
                evidence.append("rare shared tags: " + ", ".join(f"{t}({df[t]})" for t in rare))
                score += len(rare)
            ignored = [t for t in shared if t in common]
            if score < 3 and len(rare) < 2:
                continue
            high = score >= 3 and (len(rare) >= 3 or any(not e.startswith("rare") for e in evidence))
            cands.append({"neighbour": c["name"], "confidence": "high" if high else "medium",
                          "score": score, "evidence": evidence,
                          "ignored_common_tags": ignored})
        cands.sort(key=lambda p: (-p["score"], p["neighbour"]))
        result.append({"memory": o["name"], "proposals": cands[:MAX_PER_ORPHAN]})
    return {"orphans": result, "reviewed": len(reviewed & set(orphans)),
            "common_tags": sorted(common)}


def apply(db: Path, approved: list[dict]) -> dict:
    """Write approved links / standalone marks. Refuses the whole file on any stale entry."""
    with sqlite3.connect(db) as con:
        report = propose(con)
        orphans_before = len(orphan_ids(con))
    current = {(o["memory"], p["neighbour"]) for o in report["orphans"] for p in o["proposals"]}
    open_orphans = {o["memory"] for o in report["orphans"]}
    bad = []
    for a in approved:
        if "standalone" in a:
            ok = a["standalone"] in open_orphans
        else:
            ok = (a.get("memory"), a.get("neighbour")) in current
        if not ok:
            bad.append(a)
    if bad:
        raise ValueError(f"not a current proposal (already applied, reviewed, or never proposed): {bad}")
    snap = snapshot(db)
    linked = standalone = 0
    with sqlite3.connect(db) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(REVIEWS_DDL)
        for a in approved:
            if "standalone" in a:
                con.execute("INSERT OR IGNORE INTO memory_link_reviews (memory_id) "
                            "SELECT id FROM memories WHERE name=?", (a["standalone"],))
                standalone += 1
                continue
            ids = [r[0] for r in con.execute("SELECT id FROM memories WHERE name IN (?, ?)",
                                             (a["memory"], a["neighbour"]))]
            lo, hi = min(ids), max(ids)
            linked += con.execute("INSERT OR IGNORE INTO memory_links (src_id, dst_id, type) "
                                  "VALUES (?, ?, 'relates_to')", (lo, hi)).rowcount
        orphans_after = len(orphan_ids(con))
    return {"snapshot": str(snap), "linked": linked, "marked_standalone": standalone,
            "orphans_before": orphans_before, "orphans_after": orphans_after}


def recall_moves(before_db: Path, after_db: Path, n_prompts: int, top_n: int) -> list[dict]:
    """Prompts whose top-N (links boost) differs between two DB files. Fails open to []."""
    try:
        from langchain_learning.nodes._text_utils import tokenise
        import compare_memory_recall as cmr
        prompts = cmr.recent_prompts(n_prompts)
        tops = []
        for db in (before_db, after_db):
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            tops.append([[m["name"] for m in cmr.recall(conn, set(tokenise(p.lower())), "links", top_n)]
                         for p in prompts])
            conn.close()
        return [{"prompt": p[:90], "before": b, "after": a}
                for p, b, a in zip(prompts, *tops) if a != b]
    except Exception as exc:  # noqa: BLE001 — a missing event log must not undo an applied write
        return [{"error": f"recall comparison unavailable: {exc}"}]


def render(report: dict) -> str:
    lines = [f"{len(report['orphans'])} unreviewed orphans ({report['reviewed']} already reviewed standalone); "
             f"read-only, nothing written",
             f"ignored as too common: {', '.join(report['common_tags']) or 'none'}", ""]
    for o in report["orphans"]:
        if not o["proposals"]:
            lines.append(f"- {o['memory']}: no confident match")
            continue
        lines.append(f"- {o['memory']}")
        for p in o["proposals"]:
            lines.append(f"    [{p['confidence']}] {p['neighbour']}  (score {p['score']})")
            lines += [f"        {e}" for e in p["evidence"]]
            if p["ignored_common_tags"]:
                lines.append(f"        (ignored common: {', '.join(p['ignored_common_tags'])})")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", type=Path, help="JSON file of approved proposals / standalone marks")
    ap.add_argument("--prompts", type=int, default=30)
    ap.add_argument("--top-n", type=int, default=3)
    args = ap.parse_args()
    if args.apply:
        out = apply(args.db, json.loads(args.apply.read_text()))
        out["recall_moved"] = recall_moves(Path(out["snapshot"]), args.db, args.prompts, args.top_n)
        print(json.dumps(out, indent=2))
        return
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as con:
        report = propose(con)
    print(json.dumps(report, indent=2) if args.json else render(report))


if __name__ == "__main__":
    main()
