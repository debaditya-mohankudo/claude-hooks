"""General memory tools next to taskfw loop rows (task:6cca59fc)."""
import sqlite3

import pytest

from src.db.schema import MEMORIES_DDL, migrate_memory_db
from src.tools import memory


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "MEMORY.sqlite"
    con = sqlite3.connect(path)
    con.executescript(MEMORIES_DDL)
    migrate_memory_db(con)
    migrate_memory_db(con)  # idempotent
    con.close()
    monkeypatch.setattr(memory, "MEMORY_DB", path)
    return path


def _sql(path, query):
    con = sqlite3.connect(path)
    rows = con.execute(query).fetchall()
    con.commit()
    con.close()
    return rows


def test_re_add_preserves_the_new_columns_and_task_links(db):
    memory.handle_add("kept", "project", "first body", tags="x")
    _sql(db, "UPDATE memories SET kind='pitfall', scope='git:a/b', created='2026-01-01'")
    _sql(db, "INSERT INTO memory_task_links (memory_id, task_id) SELECT id, 'abc' FROM memories")
    memory.handle_add("kept", "project", "second body", tags="x")
    assert _sql(db, "SELECT origin, kind, scope, created, body FROM memories") == [
        ("general", "pitfall", "git:a/b", "2026-01-01", "second body")]
    assert _sql(db, "SELECT task_id FROM memory_task_links") == [("abc",)]
    memory.handle_delete("kept")
    assert _sql(db, "SELECT * FROM memory_task_links") == []


def test_loop_rows_are_protected_and_not_listed(db):
    memory.handle_add("lesson", "feedback", "loop lesson text", tags="gate")
    _sql(db, "UPDATE memories SET origin='loop'")
    assert "error" in memory.handle_add("lesson", "feedback", "overwrite", tags="gate")
    batch = memory.handle_add_batch([{"name": "lesson", "type": "feedback", "body": "overwrite"}])
    assert "error" in batch["results"][0]
    assert _sql(db, "SELECT body FROM memories") == [("loop lesson text",)]
    assert memory.handle_search("gate")["count"] == 0
    assert memory.handle_list()["count"] == 0
    assert memory.handle_stale(unhit_days=0, unvalidated_days=0)["total_stale"] == 0
    assert memory.handle_get("lesson")["origin"] == "loop"


def test_only_content_writes_move_updated(db):
    memory.handle_add("m", "project", "body")
    _sql(db, "CREATE TRIGGER update_timestamp AFTER UPDATE ON memories BEGIN "
             "UPDATE memories SET updated = CURRENT_TIMESTAMP WHERE id = NEW.id; END")
    con = sqlite3.connect(db)
    migrate_memory_db(con)
    con.close()
    _sql(db, "UPDATE memories SET updated='2020-01-01 00:00:00'")
    _sql(db, "UPDATE memories SET hit_count = 1, last_hit = CURRENT_TIMESTAMP")
    memory.handle_validate(["m"])
    assert _sql(db, "SELECT updated FROM memories") == [("2020-01-01 00:00:00",)]
    memory.handle_add("m", "project", "new body")
    assert _sql(db, "SELECT updated FROM memories")[0][0] > "2020-01-01 00:00:00"
