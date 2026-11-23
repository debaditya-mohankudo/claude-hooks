"""Server startup warms the memory path so the first prompt pays no build cost (task:10ff74e3).

_warm_memory_connection opens the shared MEMORY.sqlite connection (task:bc39e90e)
and builds the BM25 scoring index over it. Without the index step the first
UserPromptSubmit after a restart imported the scorer and built the index inline
(retrieve_ms 240 vs 24 steady state). No lifespan here: the function is called
directly against a plain temp database.
"""
from __future__ import annotations

import sqlite3

import pytest

from hooks.server import _warm_memory_connection
from src.config import config as base_cfg
from langchain_learning.nodes import _memory_scoring as ms
from src.db.schema import MEMORIES_DDL


@pytest.fixture
def memory_db(tmp_path):
    db = tmp_path / "memory.sqlite"
    con = sqlite3.connect(db)
    con.executescript(MEMORIES_DDL)
    for name, tags in (("warm-alpha", "alpha startup"), ("warm-beta", "beta startup")):
        con.execute("INSERT INTO memories (name, type, tags, body) VALUES (?, 'project', ?, '')", (name, tags))
    con.commit()
    con.close()
    original = base_cfg.memory_db
    object.__setattr__(base_cfg, "memory_db", db)  # Settings is frozen
    yield db
    object.__setattr__(base_cfg, "memory_db", original)


def test_startup_builds_bm25_index(memory_db):
    ms._bm25_cache.update(sig=None, bm25=None, pos={})
    _warm_memory_connection()
    assert ms._bm25_cache["bm25"] is not None
    assert len(ms._bm25_cache["pos"]) == 2


def test_warm_index_is_reused_by_first_score(memory_db):
    ms._bm25_cache.update(sig=None, bm25=None, pos={})
    _warm_memory_connection()
    warmed = ms._bm25_cache["bm25"]
    con = sqlite3.connect(memory_db)
    con.row_factory = sqlite3.Row
    try:
        ms.score_memories({"alpha", "startup"}, con, top_n=3)
    finally:
        con.close()
    assert ms._bm25_cache["bm25"] is warmed  # first prompt does not rebuild


def test_warmup_fails_open_on_unreadable_db(tmp_path):
    bad = tmp_path / "not-a-db.sqlite"
    bad.write_bytes(b"x" * 4096)
    original = base_cfg.memory_db
    object.__setattr__(base_cfg, "memory_db", bad)
    try:
        _warm_memory_connection()  # must not raise
    finally:
        object.__setattr__(base_cfg, "memory_db", original)
