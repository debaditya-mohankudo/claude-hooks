"""Shared pytest configuration — ensures all test files have the project root
and hooks/ directory on sys.path, matching the runtime environment."""
import os
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
for _p in [str(_PROJECT_ROOT), str(_PROJECT_ROOT / "hooks")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

_TEST_LOG_DB = _PROJECT_ROOT / "tests" / "test_logs.db"


def pytest_collection_modifyitems(items):
    """Enforce test execution order: api → unit → harness.

    api     validates the HTTP wire layer first (fast fail if server is broken)
    unit    core logic tests
    harness replay + diff_runs last (depends on unit run being written to test_logs.db)
    """
    api, unit, harness = [], [], []
    for item in items:
        name = item.fspath.basename
        if name == "test_server_api.py":
            api.append(item)
        elif name == "test_replay_harness.py":
            harness.append(item)
        else:
            unit.append(item)
    items[:] = api + unit + harness

# Counters for test_runs summary — populated by pytest_runtest_logreport
_run_counts: dict[str, int] = {"n_tests": 0, "n_passed": 0, "n_failed": 0}



_MEM_DB_URI_TEMPLATE = "file:testlogs_{run_id}?mode=memory&cache=shared"
# Persistent connection kept open for the entire test session so the named
# in-memory DB is not dropped between emit() calls (SQLite drops it when the
# last connection closes). Set per-session with a unique run_id in the URI.
_mem_conn: sqlite3.Connection | None = None
_MEM_DB_URI: str = ""

# Active DB for in-run queries (memory URI during run, file path for post-run tools).
# Set by test_log_db fixture at session start.
_active_log_db: str = str(_TEST_LOG_DB)
_active_log_db_uri: bool = False


def _active_connect() -> sqlite3.Connection:
    return sqlite3.connect(_active_log_db, uri=_active_log_db_uri)


@pytest.fixture(scope="session", autouse=True)
def test_log_db():
    """Accumulate all test logs in a named shared in-memory DB during the run.

    At session end, dump to tests/test_logs.db (merging into the existing file
    so history accumulates across runs). Each run is tagged with a unique run_id.

    Using :memory: avoids per-emit iCloud file writes and eliminates file locking.
    Named shared URI means all sqlite3.connect() calls within the process see the
    same in-memory DB, compatible with SQLiteHandler's per-emit open/close pattern.
    """
    import src.logger as _logger

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    _run_counts["n_tests"] = 0
    _run_counts["n_passed"] = 0
    _run_counts["n_failed"] = 0

    # Unique URI per run — prevents state bleed from a previous session's memory DB.
    global _mem_conn, _MEM_DB_URI
    _MEM_DB_URI = _MEM_DB_URI_TEMPLATE.format(run_id=run_id)

    # Open a persistent connection to keep the named in-memory DB alive for the
    # entire session. SQLite drops shared-memory DBs when all connections close —
    # without this anchor, emit() connections see an empty DB.
    _mem_conn = sqlite3.connect(_MEM_DB_URI, uri=True)
    from src.logger import _SCHEMA
    _mem_conn.executescript(_SCHEMA)
    _mem_conn.commit()

    # Point all in-run queries and all emit() calls at the memory DB.
    # CLAUDE_HOOKS_TEST_LOG_DB is read by _connect() at call time — no singleton
    # redirect needed, works regardless of import order.
    global _active_log_db, _active_log_db_uri
    _active_log_db = _MEM_DB_URI
    _active_log_db_uri = True
    os.environ["CLAUDE_HOOKS_TEST_LOG_DB"] = _MEM_DB_URI

    # Add run_id column + trigger using the persistent connection.
    cols = {row[1] for row in _mem_conn.execute("PRAGMA table_info(hook_logs)")}
    if "run_id" not in cols:
        _mem_conn.execute("ALTER TABLE hook_logs ADD COLUMN run_id TEXT")
    _mem_conn.execute("CREATE TABLE IF NOT EXISTS _run_meta (run_id TEXT)")
    _mem_conn.execute("DELETE FROM _run_meta")
    _mem_conn.execute("INSERT INTO _run_meta VALUES (?)", (run_id,))
    _mem_conn.execute("DROP TRIGGER IF EXISTS _tag_run_id")
    _mem_conn.execute("""
        CREATE TRIGGER _tag_run_id AFTER INSERT ON hook_logs
        BEGIN
            UPDATE hook_logs SET run_id = (SELECT run_id FROM _run_meta LIMIT 1)
            WHERE id = NEW.id;
        END
    """)
    _mem_conn.commit()

    yield _TEST_LOG_DB

    # Session end: write test_runs summary, dump in-memory DB → file, clear flag.
    try:
        _mem_conn.execute(
            "INSERT OR REPLACE INTO test_runs (run_id, n_tests, n_passed, n_failed) VALUES (?, ?, ?, ?)",
            (run_id, _run_counts["n_tests"], _run_counts["n_passed"], _run_counts["n_failed"]),
        )
        _mem_conn.commit()
        _merge_mem_to_file(_mem_conn, _TEST_LOG_DB)
    except Exception as exc:
        # Was a bare `pass` — silently hid a real "no such table" bug for
        # weeks (test_logs.db stayed 0 bytes since 2026-06-24). Print instead
        # so a future regression is visible in CI/deploy output, not invisible.
        print(f"WARNING: test_log_db merge-to-file failed: {exc!r}")
    finally:
        os.environ.pop("CLAUDE_HOOKS_TEST_LOG_DB", None)
        _mem_conn.close()


def _merge_mem_to_file(mem_conn: sqlite3.Connection, file_path: Path) -> None:
    """Merge in-memory run into the on-disk DB, preserving history across runs.

    Uses INSERT OR IGNORE so existing rows from prior runs are never overwritten.
    Adds run_id column to file DB if missing (first run after schema change).
    """
    with sqlite3.connect(str(file_path)) as file_conn:
        # Initialize schema if this is a fresh/empty file — without this, a new
        # file_path has no hook_logs table at all, and the ALTER TABLE below
        # throws "no such table", silently swallowed by this fixture's caller.
        from src.logger import _SCHEMA
        file_conn.executescript(_SCHEMA)

        # Ensure file DB has run_id column
        cols = {row[1] for row in file_conn.execute("PRAGMA table_info(hook_logs)")}
        if "run_id" not in cols:
            file_conn.execute("ALTER TABLE hook_logs ADD COLUMN run_id TEXT")

        # Drop the run_id trigger if it leaked into the file DB — the trigger is
        # memory-only and would overwrite run_id on INSERT with a stale _run_meta value.
        file_conn.execute("DROP TRIGGER IF EXISTS _tag_run_id")

        # Copy hook_logs rows from memory — skip any id collisions (shouldn't happen,
        # but guards against running two sessions against the same file concurrently).
        rows = mem_conn.execute(
            "SELECT logger, level, message, ts, run_id FROM hook_logs"
        ).fetchall()
        file_conn.executemany(
            "INSERT INTO hook_logs (logger, level, message, ts, run_id) VALUES (?,?,?,?,?)",
            rows,
        )

        # Copy test_runs row
        runs = mem_conn.execute("SELECT * FROM test_runs").fetchall()
        file_conn.executemany(
            "INSERT OR REPLACE INTO test_runs VALUES (?,?,?,?,?)", runs
        )


def pytest_runtest_logreport(report):
    """Accumulate pass/fail counts for the test_runs summary row."""
    if report.when != "call":
        return
    _run_counts["n_tests"] += 1
    if report.passed:
        _run_counts["n_passed"] += 1
    elif report.failed or report.outcome == "error":
        _run_counts["n_failed"] += 1


@pytest.fixture(scope="function", autouse=True)
def _log_test_marker(request, test_log_db):
    """Write TEST_START sentinel before each test.

    Yields a scoped query callable — use it instead of query_test_logs()
    when you want rows scoped to this test only.

    Usage:
        def test_something(_log_test_marker):
            ...
            rows = _log_test_marker(logger="lc.hooks.gates", search="name_arg_check")
    """
    import src.logger as _logger

    sentinel_logger = _logger.setup("pytest.marker", level=10)  # DEBUG
    sentinel_logger.info("[TEST_START] %s", request.node.nodeid)

    # Record the id of the sentinel row so we can scope queries to this test
    with _active_connect() as conn:
        start_id = conn.execute("SELECT MAX(id) FROM hook_logs").fetchone()[0] or 0

    def _query(logger: str = "", search: str | list[str] = "", level: str = "") -> list[dict]:
        """Query logs written during this test only (after the TEST_START sentinel)."""
        clauses = ["id > ?"]
        params: list = [start_id]
        if logger:
            clauses.append("logger LIKE ?")
            params.append(f"%{logger}%")
        searches = [search] if isinstance(search, str) else search
        for s in searches:
            if s:
                clauses.append("message LIKE ?")
                params.append(f"%{s}%")
        if level:
            clauses.append("level = ?")
            params.append(level.upper())
        where = f"WHERE {' AND '.join(clauses)}"
        sql = f"SELECT id, ts, level, logger, message FROM hook_logs {where} ORDER BY id"
        with _active_connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    yield _query


@pytest.fixture
def log_turn():
    """Return a callable that writes a [TURN_START] sentinel, marking a turn boundary within a test.

    Usage:
        def test_multi_turn(mem_graph, log_turn):
            log_turn("turn 1")
            sg.run_session("prompt 1", ...)
            log_turn("turn 2")
            sg.run_session("prompt 2", ...)
    """
    import src.logger as _logger

    sentinel_logger = _logger.setup("pytest.marker", level=10)

    def _mark(label: str = "") -> None:
        sentinel_logger.info("[TURN_START] %s", label)

    return _mark


def query_test_logs(logger: str = "", search: str | list[str] = "", level: str = "", limit: int = 200) -> list[dict]:
    """Query all logs for the current run (reads active DB — memory during run, file post-run).

    For scoped queries within a single test, use the _log_test_marker fixture instead.
    """
    clauses, params = [], []
    if logger:
        clauses.append("logger LIKE ?")
        params.append(f"%{logger}%")
    searches = [search] if isinstance(search, str) else search
    for s in searches:
        if s:
            clauses.append("message LIKE ?")
            params.append(f"%{s}%")
    if level:
        clauses.append("level = ?")
        params.append(level.upper())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT id, ts, level, logger, message FROM hook_logs {where} ORDER BY id DESC LIMIT {limit}"
    with _active_connect() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
