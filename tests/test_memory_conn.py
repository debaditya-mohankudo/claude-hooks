"""langchain_learning.memory_conn — one cached MEMORY.sqlite connection per process.

Pins task:bc39e90e: an encrypted open costs ~300 ms of key derivation, so the
connection is opened once and reused. What must stay true while it is reused:
writes from another connection are visible on the next read (no stale memories),
a replaced file is reopened, and an error inside the block never leaves a
half-open transaction behind.
"""
from __future__ import annotations

import os

import pytest

apsw = pytest.importorskip("apsw")

from langchain_learning import memory_conn  # noqa: E402
from src import encdb  # noqa: E402

_KEY = "ab" * 32


def _make_encrypted(path):
    raw = apsw.Connection(str(path))
    raw.pragma("cipher", "sqlcipher")
    raw.pragma("hexkey", _KEY)
    raw.execute("CREATE TABLE memories (name TEXT PRIMARY KEY, hit_count INTEGER DEFAULT 0)")
    raw.execute("INSERT INTO memories (name) VALUES ('a')")
    raw.close()


@pytest.fixture
def enc_db(tmp_path, monkeypatch):
    path = tmp_path / "MEMORY.sqlite"
    _make_encrypted(path)
    assert not encdb.is_plain(path)
    monkeypatch.setattr(encdb, "read_key", lambda *a, **k: _KEY)
    opens = []
    real_open = encdb.open_db
    monkeypatch.setattr(encdb, "open_db", lambda *a, **k: opens.append(a) or real_open(*a, **k))
    memory_conn.reset()
    yield path, opens
    memory_conn.reset()


def _names(path):
    with memory_conn.memory_connection(path) as conn:
        return sorted(r[0] for r in conn.execute("SELECT name FROM memories").fetchall())


def test_encrypted_db_is_opened_once_across_blocks(enc_db):
    path, opens = enc_db
    for _ in range(3):
        _names(path)
    assert len(opens) == 1


def test_write_from_another_connection_is_seen_through_cached_one(enc_db):
    """The stale-read check: `mac memory add` in another process must show up."""
    path, _ = enc_db
    assert _names(path) == ["a"]
    other = encdb.open_db(path)
    other.execute("INSERT INTO memories (name) VALUES ('b')")
    other.commit()
    other.close()
    assert _names(path) == ["a", "b"]


def test_writes_through_cached_connection_are_committed(enc_db):
    path, _ = enc_db
    with memory_conn.memory_connection(path) as conn:
        conn.execute("UPDATE memories SET hit_count = 5 WHERE name = 'a'")
    other = encdb.open_db(path, readonly=True)
    assert other.execute("SELECT hit_count FROM memories").fetchone()[0] == 5
    other.close()


def test_error_in_block_rolls_back_and_drops_connection(enc_db):
    path, opens = enc_db
    with pytest.raises(RuntimeError):
        with memory_conn.memory_connection(path) as conn:
            conn.execute("UPDATE memories SET hit_count = 9 WHERE name = 'a'")
            raise RuntimeError("boom")
    with memory_conn.memory_connection(path) as conn:
        assert conn.execute("SELECT hit_count FROM memories").fetchone()[0] == 0
    assert len(opens) == 2


def test_replaced_file_is_reopened(enc_db, tmp_path):
    path, opens = enc_db
    _names(path)
    fresh = tmp_path / "fresh.sqlite"
    _make_encrypted(fresh)
    os.replace(fresh, path)
    assert _names(path) == ["a"]
    assert len(opens) == 2


def test_plain_db_opens_fresh_and_commits(tmp_path):
    import sqlite3
    path = tmp_path / "plain.sqlite"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE memories (name TEXT)")
    c.commit()
    c.close()
    with memory_conn.memory_connection(path) as conn:
        conn.execute("INSERT INTO memories VALUES ('x')")
    with memory_conn.memory_connection(path, row_factory=sqlite3.Row) as conn:
        assert conn.execute("SELECT name FROM memories").fetchone()["name"] == "x"
    assert memory_conn._cached == {}
