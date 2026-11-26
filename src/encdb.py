# VENDORED COPY of ~/workspace/cli-graph/mac/encdb.py (cli-graph commit 3f2da8f, task:8b0561d1).
# Do not edit here. To re-sync: diff against the source first, then re-apply the
# two local changes marked "VENDOR:" below (KEY_FILE and the key-file docstring line).
"""One opener for personal SQLite files that may be SQLCipher-encrypted.

    from encdb import open_db
    con = open_db("~/.claude/MEMORY.sqlite")                  # read-write
    con = open_db("~/.claude/MEMORY.sqlite", readonly=True)   # read-only

A file that is plain SQLite (or does not exist yet) opens with the stdlib
`sqlite3`, so tests and unmigrated DBs keep working. Any other file is treated
as ciphertext and opened with apsw-sqlite3mc (cipher=sqlcipher), keyed right
after connect and verified with one read. Every failure raises EncDBError with
a message that says what to fix.

The key is the 64-hex-character line in secrets/memory.key, a git-crypt file
in the cli-graph repo (VENDOR: path set by KEY_FILE below). The opener fails
closed when the file is missing or is still git-crypt ciphertext.

The encrypted connection is a thin sqlite3-shaped adapter (execute, executemany,
executescript, commit, rollback, close, cursor, row_factory, context manager; cursors carry
rowcount and lastrowid, and apsw errors surface as sqlite3.OperationalError/IntegrityError);
anything beyond that is not supported.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# VENDOR: the key lives in the cli-graph repo, not next to this copy. Override with $ENCDB_KEY_FILE.
KEY_FILE = Path(os.environ.get("ENCDB_KEY_FILE", "~/workspace/cli-graph/secrets/memory.key")).expanduser()
PLAIN_HEADER = b"SQLite format 3\x00"
BUSY_TIMEOUT_MS = 5000  # how long to wait on another writer's lock
GITCRYPT_MAGIC = b"\x00GITCRYPT"
_HEX = set("0123456789abcdefABCDEF")

_key_cache: dict[Path, str] = {}


class EncDBError(RuntimeError):
    """The encrypted DB cannot be opened; the message says why."""


def is_plain(path: Path) -> bool:
    """True when the file is absent, empty, or plain SQLite."""
    try:
        with open(path, "rb") as f:
            head = f.read(len(PLAIN_HEADER))
    except FileNotFoundError:
        return True
    return head == b"" or head == PLAIN_HEADER


def _valid_key(k: str) -> bool:
    return len(k) == 64 and set(k) <= _HEX


def read_key(path: Path | None = None) -> str:
    """Hex key from the key file, cached for the life of the process."""
    kp = Path(path) if path else KEY_FILE
    if kp in _key_cache:
        return _key_cache[kp]
    try:
        raw = kp.read_bytes()
    except FileNotFoundError as e:
        raise EncDBError(f"key file {kp} is missing") from e
    if raw.startswith(GITCRYPT_MAGIC):
        raise EncDBError(f"key file {kp} is still git-crypt ciphertext; run `git-crypt unlock`")
    key = raw.decode("ascii", "replace").strip()
    if not _valid_key(key):
        raise EncDBError(f"key file {kp} does not hold 64 hex characters")
    _key_cache[kp] = key
    return key


_DML = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _translate(exc: Exception) -> Exception:
    """apsw error -> the sqlite3 class callers already catch (e.g. a bad FTS5 query)."""
    apsw = _binding()
    if isinstance(exc, apsw.ConstraintError):
        return sqlite3.IntegrityError(str(exc))
    if isinstance(exc, apsw.Error):
        return sqlite3.OperationalError(str(exc))
    return exc


class _Cursor:
    def __init__(self, con: "_Conn", cur, dml: bool = False):
        self._con, self._cur = con, cur
        self._names: list[str] | None = None
        self.rowcount = con._raw.changes() if dml else -1
        self.lastrowid = con._raw.last_insert_rowid() if dml else None

    def _wrap(self, row):
        rf = self._con.row_factory
        if row is None or rf is None:
            return row
        if self._names is None:
            self._names = [d[0] for d in self._cur.getdescription()]
        return _Row(self._names, row) if rf is sqlite3.Row else rf(self, row)

    def fetchone(self):
        try:
            return self._wrap(next(self._cur, None))
        except Exception as e:
            raise _translate(e) from e

    def fetchall(self):
        try:
            return [self._wrap(r) for r in self._cur]
        except Exception as e:
            raise _translate(e) from e

    def __iter__(self):
        try:
            for r in self._cur:
                yield self._wrap(r)
        except Exception as e:
            raise _translate(e) from e

    @property
    def description(self):
        return tuple((d[0], None, None, None, None, None, None) for d in self._cur.getdescription())


class _Row(tuple):
    """tuple with name lookup, the part of sqlite3.Row the callers use."""

    def __new__(cls, names, values):
        r = super().__new__(cls, values)
        r._names = list(names)
        return r

    def __getitem__(self, i):
        return super().__getitem__(self._names.index(i) if isinstance(i, str) else i)

    def keys(self):
        return list(self._names)


class _Conn:
    """sqlite3-shaped view of an apsw connection; writes open a transaction lazily."""

    def __init__(self, raw):
        self._raw = raw
        self.row_factory = None

    def execute(self, sql, params=()):
        dml = sql.lstrip()[:6].upper() in _DML
        try:
            if not self._raw.in_transaction and dml:
                self._raw.execute("BEGIN")
            return _Cursor(self, self._raw.execute(sql, params if isinstance(params, dict) else tuple(params)), dml)
        except Exception as e:
            raise _translate(e) from e

    def executemany(self, sql, seq):
        try:
            if not self._raw.in_transaction:
                self._raw.execute("BEGIN")
            return _Cursor(self, self._raw.executemany(sql, [tuple(p) for p in seq]), True)
        except Exception as e:
            raise _translate(e) from e

    def executescript(self, script):
        self.commit()
        try:
            return _Cursor(self, self._raw.execute(script))
        except Exception as e:
            raise _translate(e) from e

    def cursor(self):
        return self

    def commit(self):
        try:
            if self._raw.in_transaction:
                self._raw.execute("COMMIT")
        except Exception as e:
            raise _translate(e) from e

    def rollback(self):
        try:
            if self._raw.in_transaction:
                self._raw.execute("ROLLBACK")
        except Exception as e:
            raise _translate(e) from e

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        self.rollback() if et else self.commit()
        return False


def _binding():
    try:
        import apsw  # type: ignore
    except ImportError as e:
        raise EncDBError(
            "no SQLCipher binding: `uv add apsw-sqlite3mc`; the file is encrypted "
            "and plain sqlite3 cannot read it"
        ) from e
    return apsw


def open_db(path: str | Path, *, readonly: bool = False, key: str | None = None, **kwargs):
    """Open `path`, decrypting with the key file when it is not plain SQLite."""
    p = Path(path).expanduser()
    if is_plain(p):
        if readonly:
            return sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True, **kwargs)
        return sqlite3.connect(str(p), **kwargs)

    apsw = _binding()
    hexkey = key or read_key()
    if not _valid_key(hexkey):
        raise EncDBError("key is not 64 hex characters")
    flags = apsw.SQLITE_OPEN_READONLY if readonly else apsw.SQLITE_OPEN_READWRITE
    raw = apsw.Connection(str(p), flags=flags)
    try:
        # Before the key check: that read is the first to meet another writer's lock.
        raw.set_busy_timeout(BUSY_TIMEOUT_MS)
        raw.pragma("cipher", "sqlcipher")
        raw.pragma("hexkey", hexkey)
        raw.execute("SELECT count(*) FROM sqlite_master").fetchall()
    except Exception as e:
        raw.close()
        raise EncDBError(f"{p.name}: wrong key or not a SQLCipher database ({e})") from e
    return _Conn(raw)
