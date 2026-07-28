"""One-off migration: move claude-hooks-domain project/reference memories out
of the global ~/.claude/MEMORY.sqlite into the repo-local committed store
(repo_memory/memories.json), per task:850ddd65.

Excludes cross-repo-principle memories explicitly listed in _KEEP_GLOBAL —
those are true regardless of which repo you're in and stay in the global
store rather than being localized to claude-hooks.

Usage:
    uv run python scripts/migrate_repo_memories.py --dry-run
    uv run python scripts/migrate_repo_memories.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from repo_memory.store import RepoMemoryStore  # noqa: E402

MEMORY_DB = Path.home() / ".claude" / "MEMORY.sqlite"
REPO_STORE = _ROOT / "repo_memory" / "memories.json"
DOMAIN = "claude-hooks"

# Cross-repo-general facts mistagged under domain=claude-hooks — stay global.
_KEEP_GLOBAL = {
    "no-public-claude-tokenizer-use-cl100k-approx",
    "claude-cli-safe-mode-no-tools-flags",
}


def _fetch_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, type, tags, body, files, related, last_validated "
        "FROM memories WHERE domain = ? AND type IN ('project', 'reference') "
        "ORDER BY name",
        (DOMAIN,),
    ).fetchall()
    return [r for r in rows if r["name"] not in _KEEP_GLOBAL]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
    candidates = _fetch_candidates(conn)
    conn.close()

    print(f"Found {len(candidates)} candidates (domain={DOMAIN}, type in project/reference, "
          f"excluding {len(_KEEP_GLOBAL)} kept-global cross-repo facts)")

    if args.dry_run or not args.apply:
        for row in candidates:
            print(f"  {row['name']} ({row['type']})")
        print("\nDry run only. Pass --apply to migrate.")
        return

    store = RepoMemoryStore(REPO_STORE)
    migrated = []
    for row in candidates:
        store.upsert({
            "name": row["name"],
            "type": row["type"],
            "body": row["body"] or "",
            "tags": row["tags"] or "",
            "files": row["files"] or "",
            "related": row["related"] or "",
        })
        migrated.append(row["name"])

    # Delete migrated rows from the global store (+ their embeddings index entries).
    write_conn = sqlite3.connect(str(MEMORY_DB))
    try:
        from scripts.build_memories_embeddings import remove_memory
    except Exception:
        remove_memory = None
    for name in migrated:
        write_conn.execute("DELETE FROM memories WHERE name = ?", (name,))
        if remove_memory is not None:
            try:
                remove_memory(name)
            except Exception as exc:
                print(f"  warning: embedding removal failed for {name}: {exc}")
    write_conn.commit()
    write_conn.close()

    print(f"Migrated {len(migrated)} memories to {REPO_STORE}")
    print(f"Deleted {len(migrated)} rows from {MEMORY_DB}")


if __name__ == "__main__":
    main()
