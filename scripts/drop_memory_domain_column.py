"""One-time migration: fold MEMORY.sqlite's domain column into tags, then drop it.

schema.py's migrate_memory_db() is additive-only by invariant (CREATE TABLE IF
NOT EXISTS / ALTER TABLE ADD COLUMN), so a column drop deliberately lives here
instead, run once by hand:

    uv run python scripts/drop_memory_domain_column.py [--db PATH] [--dry-run]

For every row, if `domain` is non-empty and not already present as a tag token,
appends it to `tags` (comma-separated) before dropping the column — so a memory
that only ever earned a retrieval slot via domain weight doesn't go silently
untagged (see task:4c9c21e6 grooming: this is the reversibility gap the domain
axis exit needs to close, not just the schema change).

Backup first: cp ~/.claude/MEMORY.sqlite ~/.claude/MEMORY.sqlite.bak-<date>
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = Path.home() / ".claude" / "MEMORY.sqlite"


def fold_domain_into_tags(conn: sqlite3.Connection, dry_run: bool) -> int:
    rows = conn.execute("SELECT id, domain, tags FROM memories").fetchall()
    updated = 0
    for row_id, domain, tags in rows:
        if not domain:
            continue
        existing = [t.strip() for t in (tags or "").split(",") if t.strip()]
        if domain in existing:
            continue
        new_tags = ", ".join(existing + [domain])
        updated += 1
        if not dry_run:
            conn.execute("UPDATE memories SET tags = ? WHERE id = ?", (new_tags, row_id))
    return updated


def drop_domain_column(conn: sqlite3.Connection) -> None:
    # SQLite ALTER TABLE DROP COLUMN requires 3.35+; rebuild if unsupported.
    try:
        conn.execute("ALTER TABLE memories DROP COLUMN domain")
    except sqlite3.OperationalError:
        conn.execute("""
            CREATE TABLE memories_new (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT UNIQUE NOT NULL,
                type           TEXT NOT NULL,
                tags           TEXT DEFAULT '',
                body           TEXT DEFAULT '',
                related        TEXT DEFAULT '',
                updated        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_validated TIMESTAMP,
                files          TEXT,
                docs           TEXT,
                hit_count      INTEGER DEFAULT 0,
                last_hit       TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT INTO memories_new
            (id, name, type, tags, body, related, updated, last_validated, files, docs, hit_count, last_hit)
            SELECT id, name, type, tags, body, related, updated, last_validated, files, docs, hit_count, last_hit
            FROM memories
        """)
        conn.execute("DROP TABLE memories")
        conn.execute("ALTER TABLE memories_new RENAME TO memories")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"no such db: {args.db}")

    conn = sqlite3.connect(args.db)
    try:
        folded = fold_domain_into_tags(conn, args.dry_run)
        print(f"{'would fold' if args.dry_run else 'folded'} domain into tags for {folded} row(s)")
        if args.dry_run:
            print("dry-run: not dropping domain column")
            return
        conn.commit()
        drop_domain_column(conn)
        conn.commit()
        print("dropped domain column from memories")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
