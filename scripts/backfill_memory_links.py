"""Copy legacy memories.related slugs into memory_links as relates_to edges (task:5f08a3b8).

Copy, not move: `related` is never modified. Idempotent (INSERT OR IGNORE on the
canonical src_id < dst_id pair). Slugs naming no memory are skipped, never guessed
at, and written to a report for a per-slug decision (repoint, drop or leave).

    uv run python scripts/backfill_memory_links.py [--db PATH] [--report PATH] [--dry-run]

A snapshot (VACUUM INTO + PRAGMA integrity_check) is taken first unless --dry-run.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.db.schema import migrate_memory_db  # noqa: E402

DEFAULT_DB = Path.home() / ".claude" / "MEMORY.sqlite"


def snapshot(db: Path) -> Path:
    dest = db.with_name(f"{db.name}.pre-links.{datetime.now():%Y%m%dT%H%M%S}")
    with sqlite3.connect(db) as con:
        con.execute("VACUUM INTO ?", (str(dest),))
    with sqlite3.connect(dest) as snap:
        verdict = snap.execute("PRAGMA integrity_check").fetchone()[0]
    if verdict != "ok":
        raise RuntimeError(f"snapshot {dest} failed integrity_check: {verdict}")
    return dest


def backfill(db: Path, report: Path | None = None, dry_run: bool = False) -> dict:
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys = ON")
    migrate_memory_db(con)
    ids = dict(con.execute("SELECT name, id FROM memories"))
    pairs: set[tuple[int, int]] = set()
    dangling: list[tuple[str, str]] = []
    for name, related in con.execute("SELECT name, related FROM memories WHERE related != ''").fetchall():
        for slug in (s.strip() for s in related.split(",")):
            if not slug:
                continue
            if slug not in ids or slug == name:
                dangling.append((name, slug))
                continue
            pairs.add((min(ids[name], ids[slug]), max(ids[name], ids[slug])))
    before = con.execute("SELECT COUNT(*) FROM memory_links WHERE type='relates_to'").fetchone()[0]
    if not dry_run:
        con.executemany(
            "INSERT OR IGNORE INTO memory_links (src_id, dst_id, type) VALUES (?, ?, 'relates_to')",
            sorted(pairs),
        )
        con.commit()
    after = con.execute("SELECT COUNT(*) FROM memory_links WHERE type='relates_to'").fetchone()[0]
    con.close()
    if report and dangling:
        report.write_text(
            "# memory, dangling slug  (decide per slug: repoint, drop or leave)\n"
            + "".join(f"{m}\t{s}\n" for m, s in sorted(dangling))
        )
    return {"valid_unordered_pairs": len(pairs), "added": after - before,
            "total_relates_to": after, "dangling": len(dangling)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--report", type=Path, default=Path("memory_links_dangling.tsv"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.dry_run:
        print("snapshot:", snapshot(a.db))
    print(backfill(a.db, a.report, a.dry_run))
