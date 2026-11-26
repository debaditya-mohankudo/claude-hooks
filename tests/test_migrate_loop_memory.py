"""The loop-lesson cut-over (task:59d8a962), on plain temporary databases."""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

import migrate_loop_memory as mig  # noqa: E402
from db.schema import MEMORIES_DDL, migrate_memory_db  # noqa: E402

TASKS_DDL = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, scope TEXT);
CREATE TABLE memories (slug TEXT PRIMARY KEY, kind TEXT, text TEXT, superseded_by TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT, hit_count INTEGER DEFAULT 0, last_hit TEXT);
CREATE TABLE memory_links (slug TEXT, task_id TEXT, relation TEXT, created_at TEXT);
INSERT INTO tasks VALUES ('t1', 'git:github.com/O/Repo'), ('t2', '');
INSERT INTO memories VALUES
  ('old', 'pitfall', 'old text', 'new', '2026-01-01', '2026-01-02', 2, '2026-02-01'),
  ('new', 'technique', 'new text', '', '2026-01-03', '2026-01-03', 0, NULL),
  ('both', 'constraint', 'lesson text', '', '2026-01-04', '2026-01-04', 3, '2026-03-01');
INSERT INTO memory_links VALUES ('old', 't1', 'learned_from', '2026-01-01'),
  ('new', 't2', 'learned_from', '2026-01-03'), ('both', 't1', 'confirmed_by', '2026-01-05');
"""


@pytest.fixture
def dbs(tmp_path):
    task_db, memory_db = tmp_path / "tasks.db", tmp_path / "MEMORY.sqlite"
    with sqlite3.connect(task_db) as c:
        c.executescript(TASKS_DDL)
    c = sqlite3.connect(memory_db)
    c.execute(MEMORIES_DDL)
    migrate_memory_db(c)
    c.execute("INSERT INTO memories (name, type, body, updated, hit_count) "
              "VALUES ('both', 'project', 'general text', '2026-01-01', 5)")
    c.commit()
    c.close()
    return task_db, memory_db


def _rows(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_dry_run_reports_and_writes_nothing(dbs):
    report = mig.migrate(*dbs)
    assert report["merged_into_general"] == ["both"] and report["backups"] == []
    assert _rows(dbs[1], "SELECT name FROM memories") == [("both",)]
    assert _rows(dbs[0], "SELECT name FROM sqlite_master WHERE type='trigger'") == []


def test_apply_moves_rows_links_and_hits_and_keeps_the_general_row(dbs):
    report = mig.migrate(*dbs, apply=True)
    assert all(Path(p).is_file() for p in report["backups"])
    assert _rows(dbs[1], "SELECT type, body, origin, hit_count, last_hit, updated FROM memories "
                         "WHERE name='both'") == [("project", "general text", "general", 8,
                                                   "2026-03-01", "2026-01-01")]
    assert _rows(dbs[1], "SELECT origin, scope, created, updated, hit_count FROM memories "
                         "WHERE name='old'") == [("loop", "git:github.com/o/repo",
                                                  "2026-01-01", "2026-01-02", 2)]
    assert _rows(dbs[1], "SELECT s.name, d.name FROM memory_links l JOIN memories s ON s.id=l.src_id "
                         "JOIN memories d ON d.id=l.dst_id WHERE l.type='supersedes'") == [("new", "old")]
    assert len(_rows(dbs[1], "SELECT * FROM memory_task_links")) == 3


@pytest.mark.parametrize("sql", [
    "INSERT INTO memories (slug) VALUES ('late')",
    "UPDATE memories SET hit_count = 9",
    "DELETE FROM memories",
    "INSERT INTO memory_links VALUES ('new', 't2', 'confirmed_by', '2026-04-01')",
])
def test_old_tables_refuse_writes_after_cut_over(dbs, sql):
    mig.migrate(*dbs, apply=True)
    with pytest.raises(sqlite3.IntegrityError, match="moved to MEMORY.sqlite"):
        _rows(dbs[0], sql)


def test_second_run_is_refused(dbs):
    mig.migrate(*dbs, apply=True)
    with pytest.raises(mig.Refused, match="already holds loop rows"):
        mig.migrate(*dbs)
