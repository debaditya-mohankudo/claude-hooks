"""Test DB factory functions — build fixture databases from canonical schema.

All DDL comes from src/db/schema.py. Tests import these factories instead of
defining inline DDL or _make_*_db helpers.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.db.schema import (
    HOOK_LOGS_DDL,
    MEMORIES_DDL,
    MCP_TOOL_HINTS_DDL,
    TEST_RUNS_DDL,
)


def make_memory_db(tmp_path: Path, memories: list[dict] | None = None) -> Path:
    """Create a MEMORY.sqlite fixture. memories is a list of row dicts."""
    db = tmp_path / "MEMORY.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(MEMORIES_DDL)
        for m in memories or []:
            conn.execute(
                "INSERT INTO memories (name, type, domain, tags, body, files) VALUES (?,?,?,?,?,?)",
                (
                    m["name"],
                    m.get("type", "feedback"),
                    m.get("domain", "global"),
                    m.get("tags", ""),
                    m.get("body", ""),
                    m.get("files", None),
                ),
            )
        conn.commit()
    return db


# make_tasks_db was removed here (task:87ec7876), along with the DDL it built
# from — open_tasks, task_events, task_edges. Task storage lives in
# task-framework now.


def make_tool_hints_db(tmp_path: Path, hints: list[dict] | None = None) -> Path:
    """Create a tool_hints.sqlite fixture."""
    db = tmp_path / "tool_hints.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(MCP_TOOL_HINTS_DDL)
        for h in hints or []:
            conn.execute(
                "INSERT INTO mcp_tool_hints (tool_name, domain, count, keywords, skill) VALUES (?,?,?,?,?)",
                (
                    h["tool_name"],
                    h.get("domain", ""),
                    h.get("count", 0),
                    h.get("keywords", ""),
                    h.get("skill", ""),
                ),
            )
        conn.commit()
    return db


def make_hooks_db(tmp_path: Path) -> Path:
    """Create a claude_hooks.sqlite fixture."""
    db = tmp_path / "claude_hooks.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(HOOK_LOGS_DDL)
        conn.executescript(TEST_RUNS_DDL)
        conn.commit()
    return db
