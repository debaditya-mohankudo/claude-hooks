"""Central SQLite DDL definitions and migrate() functions.

Purpose: this module is NOT the production schema/migration path. Every tool
module (memory.py, scratch.py, hooks.py) connects to its own DB directly and
manages its own schema independently — most assume scripts/init_db.py already
ran once. This split is deliberate (task:cb357eb6, commit de1ae61, 2026-06-27):
it lets test fixtures and the one-time installer share DDL without giving
schema.py any runtime authority over production connect-time behavior.

Usage:
- Tests: import DDL constants to build fixtures (no inline DDL in test files).
- Setup: call migrate_*() once on first install via scripts/init_db.py.
- Prod connect-time code (_ensure_db, _SCHEMA, etc.) is NOT replaced — it stays as-is.

Adding a column/table: add it to the DDL constant + migrate_*() here, AND to any
tool module's own inline schema code if that module self-heals — the two must
be kept in sync by hand.
"""
from __future__ import annotations

import sqlite3

# ── MEMORY.sqlite ─────────────────────────────────────────────────────────────

MEMORIES_DDL = """
CREATE TABLE IF NOT EXISTS memories (
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
"""


def migrate_memory_db(conn: sqlite3.Connection) -> None:
    """Additive migrations for MEMORY.sqlite."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    additive = {
        "related":        "TEXT DEFAULT ''",
        "last_validated": "TIMESTAMP",
        "files":          "TEXT",
        "docs":           "TEXT",
        "hit_count":      "INTEGER DEFAULT 0",
        "last_hit":       "TIMESTAMP",
    }
    for col, typedef in additive.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {typedef}")
    conn.commit()


# ── proj_tasks.db ─────────────────────────────────────────────────────────────
#
# open_tasks, task_events, commit_task_map, task_edges and migrate_tasks_db
# removed here (task:87ec7876), along with src/tools/tasks.py — the actual
# production path that created these tables, this module's docstring having
# always said the constants below existed only for tests and one-time install.
# Task storage lives in task-framework now; this repo migrated its own history
# there rather than keeping a second copy (task:75ade3a8, 1406 rows).

# ── tool_hints.sqlite ─────────────────────────────────────────────────────────

MCP_TOOL_HINTS_DDL = """
CREATE TABLE IF NOT EXISTS mcp_tool_hints (
    tool_name      TEXT PRIMARY KEY,
    count          INTEGER DEFAULT 0,
    last_used      TIMESTAMP,
    avg_latency_ms REAL DEFAULT 0.0,
    keywords       TEXT DEFAULT '',
    skill          TEXT DEFAULT '',
    recent_prompts TEXT DEFAULT '[]',
    embedding      BLOB
)
"""


def migrate_tool_hints_db(conn: sqlite3.Connection) -> None:
    """Additive migrations for tool_hints.sqlite."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mcp_tool_hints)")}
    additive = {
        "skill":          "TEXT DEFAULT ''",
        "recent_prompts": "TEXT DEFAULT '[]'",
        "embedding":      "BLOB",
    }
    for col, typedef in additive.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE mcp_tool_hints ADD COLUMN {col} {typedef}")
    conn.commit()


# ── claude_hooks.sqlite ───────────────────────────────────────────────────────

HOOK_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS hook_logs (
    id      INTEGER PRIMARY KEY,
    logger  TEXT      NOT NULL,
    level   TEXT      NOT NULL,
    message TEXT      NOT NULL,
    ts      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

TEST_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS test_runs (
    run_id   TEXT PRIMARY KEY,
    ts       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    n_tests  INTEGER NOT NULL DEFAULT 0,
    n_passed INTEGER NOT NULL DEFAULT 0,
    n_failed INTEGER NOT NULL DEFAULT 0
)
"""


def migrate_hooks_db(conn: sqlite3.Connection) -> None:
    """Additive migrations for claude_hooks.sqlite."""
    # No additive columns yet — placeholder for future migrations.
    conn.commit()
