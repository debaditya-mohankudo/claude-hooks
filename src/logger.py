"""Shared logger for hooks and the LangChain pipeline.

Two usage patterns:

Hook scripts (immediate write):
    from src.logger import setup
    log = setup("my_hook")
    log.info("done")

LCEL pipeline (also immediate — FastAPI server is long-lived, no flush needed):
    from src.logger import get_logger
    _log = get_logger(__name__)   # → "lc.<name>"
"""
import logging
import os
import sqlite3
import threading

from src.config import config as _cfg

_LOG_DB = _cfg.log_db

# When CLAUDE_HOOKS_TEST_LOG_DB env var is set, all emit() calls write there.
# conftest sets this before any imports — prod code never touches it.
_TEST_LOG_DB: str | None = os.environ.get("CLAUDE_HOOKS_TEST_LOG_DB") or None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hook_logs (
    id      INTEGER PRIMARY KEY,
    logger  TEXT      NOT NULL,
    level   TEXT      NOT NULL,
    message TEXT      NOT NULL,
    ts      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS test_runs (
    run_id   TEXT PRIMARY KEY,
    ts       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    n_tests  INTEGER NOT NULL DEFAULT 0,
    n_passed INTEGER NOT NULL DEFAULT 0,
    n_failed INTEGER NOT NULL DEFAULT 0
);
"""


class SQLiteHandler(logging.Handler):
    """Singleton per db_path — opens a fresh connection on each emit."""

    _instances: dict[str, "SQLiteHandler"] = {}

    def __new__(cls, db_path=None):
        key = str(db_path or _LOG_DB)
        if key not in cls._instances:
            instance = super().__new__(cls)
            cls._instances[key] = instance
        return cls._instances[key]

    def __init__(self, db_path=None):
        if hasattr(self, "_initialized"):
            return
        super().__init__()
        self._db_path = str(db_path or _LOG_DB)
        self._lock = threading.Lock()
        self._initialized = True
        self._ensure_schema()

    @classmethod
    def instance(cls) -> "SQLiteHandler":
        """Return the default singleton (keyed on _LOG_DB)."""
        return cls()

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist. Never raises."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(_SCHEMA)
        except Exception:
            pass

    def redirect(self, db_path: str) -> None:
        """Redirect writes to a new path and update the _instances registry.

        Updating _instances ensures that future instance() calls return this
        same object (keyed on new path), preventing duplicate handlers on loggers.
        Used by conftest to redirect prod-DB singletons to an in-memory test DB.
        """
        with self._lock:
            old_key = self._db_path
            self._db_path = db_path
            self._ensure_schema()
            # Re-key in _instances so future instance() lookups hit this object.
            cls = type(self)
            if old_key in cls._instances and cls._instances[old_key] is self:
                del cls._instances[old_key]
            cls._instances[db_path] = self

    def _connect(self) -> sqlite3.Connection:
        # Re-read env var at call time so conftest can set it after import.
        # Only redirect the default singleton — handlers with an explicit custom
        # path (e.g. test_logger's _fresh_handler) write to their own DB.
        path = self._db_path
        if path == str(_LOG_DB):
            override = os.environ.get("CLAUDE_HOOKS_TEST_LOG_DB") or _TEST_LOG_DB
            if override:
                path = override
        is_uri = path.startswith("file:")
        return sqlite3.connect(path, uri=is_uri)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self._lock:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO hook_logs (logger, level, message) VALUES (?, ?, ?)",
                        (record.name, record.levelname, msg),
                    )
                    count = conn.execute(
                        "SELECT COUNT(*) FROM hook_logs"
                    ).fetchone()[0]
                    if count >= 50_000:
                        conn.execute(
                            "DELETE FROM hook_logs WHERE id NOT IN "
                            "(SELECT id FROM hook_logs ORDER BY ts DESC LIMIT 40000)"
                        )
        except Exception:
            pass

    def handleError(self, record: logging.LogRecord) -> None:
        pass


def setup(name: str, level: int = logging.INFO) -> logging.Logger:
    """Return a logger with the shared SQLiteHandler attached (immediate writes)."""
    logger = logging.getLogger(name)
    h = SQLiteHandler.instance()
    if h not in logger.handlers:
        logger.addHandler(h)
    logger.setLevel(level)
    return logger


def get_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    """Return a logger prefixed with 'lc.' for the LCEL pipeline (immediate writes)."""
    lc_name = f"lc.{name}" if not name.startswith("lc.") else name
    logger = logging.getLogger(lc_name)
    h = SQLiteHandler.instance()
    if h not in logger.handlers:
        logger.addHandler(h)
    logger.setLevel(level)
    return logger
