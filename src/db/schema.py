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

# Typed memory-to-memory links (task:5f08a3b8). Edges key on memories.id, so an
# upsert (ON CONFLICT DO UPDATE) keeps them. The closed type set is the
# edge_types table: adding a type is one INSERT. Enforcement is by triggers as
# well as FKs, because SQLite FKs are per-connection and off by default (the
# sqlite3 CLI and bare sqlite3.connect() skip them); triggers fire regardless.
# Symmetric types (relates_to, contradicts) are stored once with src_id < dst_id
# and read from either end. Do not rebuild `memories` (CREATE/INSERT/DROP/RENAME)
# without recreating these triggers: the rebuild would orphan the edges.
MEMORY_LINKS_DDL = """
CREATE TABLE IF NOT EXISTS edge_types (
    type      TEXT PRIMARY KEY,
    symmetric INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO edge_types (type, symmetric) VALUES
    ('relates_to', 1), ('contradicts', 1),
    ('supersedes', 0), ('learned_from', 0), ('extends', 0);
CREATE TABLE IF NOT EXISTS memory_links (
    src_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    dst_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    type    TEXT NOT NULL REFERENCES edge_types(type),
    created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (src_id, dst_id, type),
    CHECK (src_id != dst_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_links_dst ON memory_links(dst_id);
CREATE TRIGGER IF NOT EXISTS memory_links_validate
BEFORE INSERT ON memory_links
BEGIN
    SELECT RAISE(ABORT, 'unknown link type')
      WHERE NOT EXISTS (SELECT 1 FROM edge_types WHERE type = NEW.type);
    SELECT RAISE(ABORT, 'link endpoint memory does not exist')
      WHERE NOT EXISTS (SELECT 1 FROM memories WHERE id = NEW.src_id)
         OR NOT EXISTS (SELECT 1 FROM memories WHERE id = NEW.dst_id);
    SELECT RAISE(ABORT, 'symmetric link must be stored with src_id < dst_id')
      WHERE NEW.src_id > NEW.dst_id
        AND (SELECT symmetric FROM edge_types WHERE type = NEW.type) = 1;
END;
CREATE TRIGGER IF NOT EXISTS memory_links_cascade
BEFORE DELETE ON memories
BEGIN
    DELETE FROM memory_links WHERE src_id = OLD.id OR dst_id = OLD.id;
END;
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
    conn.executescript(MEMORY_LINKS_DDL)
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


# One row per find_cli lookup, linked to the next `mac` call in the same session
# (task:cf5d016d). src/tools/find_cli_log.py creates it on connect from this
# constant, so there is no second copy to keep in sync.
FIND_CLI_QUERIES_DDL = """
CREATE TABLE IF NOT EXISTS find_cli_queries (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,          -- unix time of the find_cli call
    session_id  TEXT    NOT NULL,
    query       TEXT    NOT NULL,
    seeds       TEXT    NOT NULL,          -- JSON list of command ids, best first
    neighbours  TEXT    NOT NULL,          -- JSON list of command ids
    chosen      TEXT,                      -- the mac command run next, or NULL
    chosen_rank INTEGER,                   -- 1-based seed rank of chosen, NULL if not a seed
    outcome     TEXT,                      -- seed | neighbour | miss; NULL = no mac call followed
    linked_ts   REAL
);
CREATE INDEX IF NOT EXISTS find_cli_queries_session ON find_cli_queries(session_id, ts);
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
