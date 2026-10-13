"""suggest_memory_edge_types: proposals only on existing relates_to pairs; apply is safe (task:5484cd03)."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import suggest_memory_edge_types as sm  # noqa: E402
from src.db.schema import MEMORIES_DDL, migrate_memory_db  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "m.sqlite"
    con = sqlite3.connect(path)
    con.executescript(MEMORIES_DDL)
    migrate_memory_db(con)
    con.close()
    return path


def seed(db, memories, edges):
    with sqlite3.connect(db) as con:
        for n, b in memories.items():
            con.execute("INSERT INTO memories (name, type, body) VALUES (?, 'project', ?)", (n, b))
        for a, b in edges:
            ia, ib = (con.execute("SELECT id FROM memories WHERE name=?", (x,)).fetchone()[0] for x in (a, b))
            con.execute("INSERT INTO memory_links (src_id, dst_id, type) VALUES (?,?,'relates_to')", (ia, ib))


def props(db):
    with sqlite3.connect(db) as con:
        return sm.propose(con)


def test_replaces_quoted_slug_makes_owner_the_superseder(db):
    seed(db, {"new": "This replaces old note [[old]].", "old": "x"}, [("new", "old")])
    assert [(p["src"], p["dst"], p["type"]) for p in props(db)] == [("new", "old", "supersedes")]


def test_superseded_by_makes_owner_the_old_one(db):
    seed(db, {"a": "Superseded by [[b]].", "b": "x"}, [("a", "b")])
    assert [(p["src"], p["dst"]) for p in props(db)] == [("b", "a")]


def test_ambiguous_direction_proposes_nothing(db):
    seed(db, {"a": "Superseded (2026-07-28) see [[b]], which replaces it.", "b": "x"}, [("a", "b")])
    assert props(db) == []


def test_no_edge_no_proposal_and_phrase_without_slug_no_proposal(db):
    seed(db, {"a": "This replaces [[b]].", "b": "x", "c": "replaces everything", "d": "y"}, [("c", "d")])
    assert props(db) == []


def test_dated_pair_is_extends_newer_to_older(db):
    seed(db, {"run-2026-10-07-x": "a", "run-2026-10-08-x": "b"}, [("run-2026-10-07-x", "run-2026-10-08-x")])
    assert [(p["src"], p["dst"], p["type"]) for p in props(db)] == [("run-2026-10-08-x", "run-2026-10-07-x", "extends")]


def test_apply_converts_snapshots_and_is_idempotent(db):
    seed(db, {"new": "replaces [[old]]", "old": "x"}, [("new", "old")])
    approved = [{"src": "new", "dst": "old", "type": "supersedes"}]
    res = sm.apply(db, approved)
    assert Path(res["snapshot"]).exists() and res["changed"] == 1
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT type FROM memory_links").fetchall() == [("supersedes",)]
        src = con.execute("SELECT m.name FROM memory_links l JOIN memories m ON m.id=l.src_id").fetchone()[0]
    assert src == "new"
    with pytest.raises(ValueError):  # rerun: no relates_to pair left, so no longer a proposal
        sm.apply(db, approved)


def test_apply_refuses_unproposed_edge(db):
    seed(db, {"a": "x", "b": "y"}, [("a", "b")])
    with pytest.raises(ValueError):
        sm.apply(db, [{"src": "a", "dst": "b", "type": "supersedes"}])
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT type FROM memory_links").fetchall() == [("relates_to",)]


def test_contradicts_stored_symmetric(db):
    seed(db, {"zeta": "x", "alpha": "This contradicts [[zeta]]."}, [("zeta", "alpha")])
    sm.apply(db, [{"src": p["src"], "dst": p["dst"], "type": p["type"]} for p in props(db)])
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT src_id < dst_id, type FROM memory_links").fetchall() == [(1, "contradicts")]
