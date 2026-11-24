"""A plain (unencrypted) MEMORY.sqlite works without any SQLCipher machinery.

Pins the claim that another user of this repo can run it with an ordinary
SQLite memory file: reading, scoring and hit-count writes must never read the
key file or import the cipher binding. Encryption is the author's choice, not a
requirement of the pipeline.
"""
from __future__ import annotations

import sqlite3

import pytest

from langchain_learning import memory_conn
from langchain_learning.nodes import _memory_scoring as scoring
from src import encdb

_SCHEMA = (
    "CREATE TABLE memories (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, "
    "type TEXT NOT NULL DEFAULT 'feedback', domain TEXT NOT NULL DEFAULT 'global', "
    "tags TEXT, body TEXT NOT NULL, updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
    "hit_count INTEGER DEFAULT 0, last_hit TIMESTAMP, last_validated TIMESTAMP, "
    "files TEXT, docs TEXT, related TEXT)"
)


@pytest.fixture
def no_cipher(monkeypatch):
    """Any attempt to read the key or load the cipher binding fails the test."""
    def _boom(*a, **k):
        raise AssertionError("plain MEMORY.sqlite must not touch the key file or cipher binding")

    monkeypatch.setattr(encdb, "read_key", _boom)
    monkeypatch.setattr(encdb, "_binding", _boom)
    memory_conn.reset()
    yield
    memory_conn.reset()


@pytest.fixture
def plain_db(tmp_path, monkeypatch, no_cipher):
    db = tmp_path / "MEMORY.sqlite"
    con = sqlite3.connect(db)
    con.execute(_SCHEMA)
    con.execute(
        "INSERT INTO memories (name, type, tags, body) VALUES "
        "('plain-fixture-alpha', 'reference', 'plainfixture sentinel alpha', 'body a')"
    )
    con.commit()
    con.close()
    assert encdb.is_plain(db)

    from src.config import config as base_cfg
    original = base_cfg.memory_db
    object.__setattr__(base_cfg, "memory_db", db)  # Settings is frozen
    yield db
    object.__setattr__(base_cfg, "memory_db", original)


def test_plain_db_scores_without_key_or_cipher(plain_db):
    with memory_conn.memory_connection(plain_db, row_factory=sqlite3.Row) as conn:
        top = scoring.score_memories({"plainfixture", "sentinel", "alpha"}, conn)
    assert [m["name"] for m in top] == ["plain-fixture-alpha"]


def test_plain_db_hit_counts_are_recorded(plain_db):
    scoring.record_memory_hits(["plain-fixture-alpha"])
    con = sqlite3.connect(plain_db)
    (hits,) = con.execute("SELECT hit_count FROM memories WHERE name='plain-fixture-alpha'").fetchone()
    con.close()
    assert hits == 1


def test_plain_db_is_not_cached_across_blocks(plain_db):
    """A plain file opens fresh per call, so another process's writes are always seen."""
    with memory_conn.memory_connection(plain_db) as conn:
        conn.execute("INSERT INTO memories (name, body) VALUES ('added-later', 'x')")
    with memory_conn.memory_connection(plain_db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM memories")}
    assert "added-later" in names
    assert not memory_conn._cached


def test_missing_memory_file_does_not_need_key_or_cipher(tmp_path, no_cipher):
    """A fresh install has no MEMORY.sqlite yet; opening it must not demand a key."""
    path = tmp_path / "does-not-exist-yet.sqlite"
    with memory_conn.memory_connection(path) as conn:
        conn.execute("CREATE TABLE t (x)")
    assert encdb.is_plain(path)
