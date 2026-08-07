"""One-time migration: fold tool_hints.sqlite's domain column into keywords, then drop it.

schema.py's migrate_tool_hints_db() is additive-only by invariant (CREATE TABLE
IF NOT EXISTS / ALTER TABLE ADD COLUMN), so a column drop deliberately lives
here instead, run once by hand:

    uv run python scripts/drop_tool_hints_domain_column.py [--db PATH] [--dry-run]

For every row, if `domain` is non-empty and not already present as a keyword
token, appends it to `keywords` (comma-separated) before dropping the column —
mirrors scripts/drop_memory_domain_column.py's reasoning (task:4c9c21e6): a
tool that only ever earned a retrieval slot via domain weight doesn't go
silently unmatchable (task:5b15dc9b).

Backup first: cp <tool_hints.sqlite> <tool_hints.sqlite>.bak-<date>
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "Databases"
    / "tool_hints.sqlite"
)


def fold_domain_into_keywords(conn: sqlite3.Connection, dry_run: bool) -> int:
    rows = conn.execute("SELECT tool_name, domain, keywords FROM mcp_tool_hints").fetchall()
    updated = 0
    for tool_name, domain, keywords in rows:
        if not domain:
            continue
        existing = [k.strip() for k in (keywords or "").split(",") if k.strip()]
        if domain in existing:
            continue
        new_keywords = ",".join(existing + [domain])
        updated += 1
        if not dry_run:
            conn.execute(
                "UPDATE mcp_tool_hints SET keywords = ? WHERE tool_name = ?",
                (new_keywords, tool_name),
            )
    return updated


def drop_domain_column(conn: sqlite3.Connection) -> None:
    # SQLite ALTER TABLE DROP COLUMN requires 3.35+; rebuild if unsupported.
    try:
        conn.execute("ALTER TABLE mcp_tool_hints DROP COLUMN domain")
    except sqlite3.OperationalError:
        conn.execute("""
            CREATE TABLE mcp_tool_hints_new (
                tool_name      TEXT PRIMARY KEY,
                count          INTEGER DEFAULT 0,
                last_used      TIMESTAMP,
                avg_latency_ms REAL DEFAULT 0.0,
                keywords       TEXT DEFAULT '',
                skill          TEXT DEFAULT '',
                recent_prompts TEXT DEFAULT '[]',
                embedding      BLOB
            )
        """)
        conn.execute("""
            INSERT INTO mcp_tool_hints_new
            (tool_name, count, last_used, avg_latency_ms, keywords, skill, recent_prompts, embedding)
            SELECT tool_name, count, last_used, avg_latency_ms, keywords, skill, recent_prompts, embedding
            FROM mcp_tool_hints
        """)
        conn.execute("DROP TABLE mcp_tool_hints")
        conn.execute("ALTER TABLE mcp_tool_hints_new RENAME TO mcp_tool_hints")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"no such db: {args.db}")

    conn = sqlite3.connect(args.db)
    try:
        folded = fold_domain_into_keywords(conn, args.dry_run)
        print(f"{'would fold' if args.dry_run else 'folded'} domain into keywords for {folded} row(s)")
        if args.dry_run:
            print("dry-run: not dropping domain column")
            return
        conn.commit()
        drop_domain_column(conn)
        conn.commit()
        print("dropped domain column from mcp_tool_hints")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
