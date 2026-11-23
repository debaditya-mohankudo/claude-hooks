"""One long-lived connection to MEMORY.sqlite for the hook-server process.

Opening the SQLCipher-encrypted MEMORY.sqlite costs 280-390 ms per connection:
the cipher derives the page key from the passphrase on every open. The UPS path
opened it twice per prompt (scoring read + hit-count write), which was ~600 ms of
the 659 ms median UPS time measured in task:bc39e90e. Holding one connection for
the life of the process pays that cost once.

The cache lives here, not in src/encdb.py: that file is a vendored copy of
cli-graph's opener and is not edited in this repo.

    with memory_connection() as conn:          # yields a sqlite3-shaped connection
        conn.execute(...)

- One connection per path, used under one lock: apsw refuses concurrent use of a
  connection from two threads, and the hook routes run in a threadpool.
- A plain (unencrypted) file — tests and unmigrated DBs — is opened fresh per call;
  opening it is cheap and caching it would need check_same_thread=False.
- The block commits on success and rolls back on error, so a write never leaves a
  transaction (and its lock) open against `mac memory add` in another process.
- The cached connection is dropped and reopened when the file's inode changes
  (replaced on disk) and after any error inside the block.
- SQLite starts a fresh read snapshot per statement outside a transaction, so writes
  from other processes are visible on the next read; nothing here caches rows.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_lock = threading.RLock()
_cached: dict[str, tuple[int, object]] = {}   # resolved path -> (inode, connection)


def _inode(p: Path) -> int:
    try:
        return os.stat(p).st_ino
    except OSError:
        return -1


def _drop(key: str) -> None:
    entry = _cached.pop(key, None)
    if entry is not None:
        try:
            entry[1].close()  # type: ignore[attr-defined]
        except Exception:
            pass


def reset() -> None:
    """Close every cached connection (tests, server shutdown)."""
    with _lock:
        for key in list(_cached):
            _drop(key)


@contextmanager
def memory_connection(path: str | Path | None = None, *, row_factory=None) -> Iterator:
    """Yield a read-write connection to MEMORY.sqlite (or `path`), committed on exit."""
    from src.encdb import is_plain, open_db

    if path is None:
        from langchain_learning.config import config as _cfg
        path = _cfg.memory_db
    p = Path(path).expanduser()

    if is_plain(p):
        conn = open_db(p)
        conn.row_factory = row_factory
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    key = str(p.resolve())
    with _lock:
        ino = _inode(p)
        entry = _cached.get(key)
        if entry is None or entry[0] != ino:
            _drop(key)
            entry = (ino, open_db(p))
            _cached[key] = entry
        conn = entry[1]
        conn.row_factory = row_factory  # type: ignore[attr-defined]
        try:
            yield conn
            conn.commit()  # type: ignore[attr-defined]
        except BaseException:
            _drop(key)  # rollback by close; a broken connection is not reused
            raise
        finally:
            if key in _cached:
                conn.row_factory = None  # type: ignore[attr-defined]
