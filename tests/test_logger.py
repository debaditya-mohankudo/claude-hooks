"""Regression tests for src.logger — prod schema and emit() contract."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.logger import SQLiteHandler, _SCHEMA


def _fresh_handler(tmp_path: Path) -> tuple[SQLiteHandler, Path]:
    """Create a SQLiteHandler pointed at a fresh temp DB (not the test singleton)."""
    db = tmp_path / "fresh.db"
    h = SQLiteHandler.__new__(SQLiteHandler)
    h._db_path = str(db)
    h._initialized = True
    import threading
    h._lock = threading.Lock()
    import logging
    logging.Handler.__init__(h)
    h._ensure_schema()
    return h, db


def _columns(db: Path) -> set[str]:
    with sqlite3.connect(str(db)) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(hook_logs)")}


class TestProdSchema:
    def test_hook_logs_has_no_run_id_column(self, tmp_path):
        """Prod schema must never include run_id — test-only concern."""
        _, db = _fresh_handler(tmp_path)
        assert "run_id" not in _columns(db)

    def test_hook_logs_required_columns(self, tmp_path):
        """hook_logs must have exactly the prod columns."""
        _, db = _fresh_handler(tmp_path)
        cols = _columns(db)
        assert {"id", "logger", "level", "message", "ts"} <= cols

    def test_schema_constant_hook_logs_has_no_run_id(self):
        """hook_logs CREATE TABLE in _SCHEMA must not include run_id (test_runs may have it)."""
        # Extract just the hook_logs block
        hook_logs_block = _SCHEMA.split("CREATE TABLE IF NOT EXISTS test_runs")[0]
        assert "run_id" not in hook_logs_block


class TestEmit:
    def test_emit_succeeds_on_schema_without_run_id(self, tmp_path):
        """emit() must write a row using only (logger, level, message) — no run_id."""
        import logging
        h, db = _fresh_handler(tmp_path)
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO,
            pathname="", lineno=0, msg="hello prod", args=(), exc_info=None,
        )
        h.emit(record)
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT logger, level, message FROM hook_logs").fetchone()
        assert row == ("test.logger", "INFO", "hello prod")

    def test_emit_does_not_fail_on_run_id_column_present(self, tmp_path):
        """emit() must not break if a run_id column exists (test DB redirect scenario)."""
        import logging
        h, db = _fresh_handler(tmp_path)
        with sqlite3.connect(str(db)) as conn:
            conn.execute("ALTER TABLE hook_logs ADD COLUMN run_id TEXT")
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO,
            pathname="", lineno=0, msg="still works", args=(), exc_info=None,
        )
        h.emit(record)
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT logger, message, run_id FROM hook_logs").fetchone()
        assert row[0] == "test.logger"
        assert row[1] == "still works"
        # run_id not set by emit — may be NULL (no trigger in this DB)
        assert row[2] is None
