"""suggest_orphan_links: precision-first proposals, gated idempotent apply (task:d4f9a585)."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import suggest_orphan_links as so  # noqa: E402
from src.db.schema import MEMORIES_DDL, migrate_memory_db  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "m.sqlite"
    con = sqlite3.connect(path)
    con.executescript(MEMORIES_DDL)
    migrate_memory_db(con)
    con.close()
    return path


def seed(db, memories, edges=()):
    """memories: name -> (tags, body[, files])."""
    with sqlite3.connect(db) as con:
        for n, v in memories.items():
            con.execute("INSERT INTO memories (name, type, tags, body, files) VALUES (?, 'project', ?, ?, ?)",
                        (n, v[0], v[1], v[2] if len(v) > 2 else None))
        for a, b in edges:
            ids = sorted(con.execute("SELECT id FROM memories WHERE name=?", (x,)).fetchone()[0] for x in (a, b))
            con.execute("INSERT INTO memory_links (src_id, dst_id, type) VALUES (?,?,'relates_to')", tuple(ids))


def report(db):
    with sqlite3.connect(db) as con:
        return {o["memory"]: o["proposals"] for o in so.propose(con)["orphans"]}


def links(db):
    with sqlite3.connect(db) as con:
        return con.execute("SELECT src_id, dst_id FROM memory_links").fetchall()


def test_quoted_slug_proposes_with_evidence(db):
    seed(db, {"orphan-note-one": ("a", "x"), "other-note-two": ("b", "see orphan-note-one for why")})
    p = report(db)["orphan-note-one"]
    assert [x["neighbour"] for x in p] == ["other-note-two"]
    assert p[0]["confidence"] == "high" and "quoted" in p[0]["evidence"][0]


def test_single_rare_tag_is_no_match(db):
    seed(db, {"orphan-note-one": ("alpha", "x"), "other-note-two": ("alpha", "y")})
    assert report(db)["orphan-note-one"] == []


def test_very_common_tag_is_not_evidence(db):
    mems = {f"filler-{i}": ("common,t%d" % i, "z") for i in range(40)}
    mems |= {"orphan-note-one": ("common,solo", "x"), "other-note-two": ("common,solo2", "y")}
    seed(db, mems, edges=[(f"filler-{i}", f"filler-{i+1}") for i in range(39)])
    with sqlite3.connect(db) as con:
        assert "common" in so.propose(con)["common_tags"]
    assert report(db)["orphan-note-one"] == []


def test_two_rare_tags_medium_and_no_self_or_existing_edge(db):
    seed(db, {"orphan-note-one": ("alpha,beta", "x"), "other-note-two": ("alpha,beta", "y"),
              "linked-note-three": ("alpha,beta", "w"), "linked-note-four": ("q", "v")},
         edges=[("linked-note-three", "linked-note-four")])
    names = {x["neighbour"]: x["confidence"] for x in report(db)["orphan-note-one"]}
    assert "orphan-note-one" not in names
    assert names["other-note-two"] == "medium"


def test_hub_neighbour_skipped(db):
    mems = {f"leaf-{i}": ("z", "z") for i in range(so.MAX_DEGREE + 2)}
    mems |= {"hub-note-main": ("alpha,beta", "x"), "orphan-note-one": ("alpha,beta", "y")}
    seed(db, mems, edges=[("hub-note-main", f"leaf-{i}") for i in range(so.MAX_DEGREE + 2)])
    assert report(db)["orphan-note-one"] == []


def test_apply_writes_canonical_edge_snapshots_and_is_refused_on_rerun(db):
    seed(db, {"orphan-note-one": ("alpha,beta", "x"), "other-note-two": ("alpha,beta", "y")})
    out = so.apply(db, [{"memory": "orphan-note-one", "neighbour": "other-note-two"}])
    assert out["linked"] == 1 and out["orphans_before"] == 2 and out["orphans_after"] == 0
    assert Path(out["snapshot"]).exists()
    (lo, hi), = links(db)
    assert lo < hi
    before = links(db)
    with pytest.raises(ValueError):
        so.apply(db, [{"memory": "orphan-note-one", "neighbour": "other-note-two"}])
    assert links(db) == before


def test_stale_entry_refuses_whole_file_before_any_write(db):
    seed(db, {"orphan-note-one": ("alpha,beta", "x"), "other-note-two": ("alpha,beta", "y")})
    with pytest.raises(ValueError):
        so.apply(db, [{"memory": "orphan-note-one", "neighbour": "other-note-two"},
                      {"memory": "orphan-note-one", "neighbour": "nope"}])
    assert links(db) == []
    assert not list(db.parent.glob("*.pre-links.*"))


def test_standalone_is_recorded_and_not_reproposed(db):
    seed(db, {"orphan-note-one": ("alpha,beta", "x"), "other-note-two": ("alpha,beta", "y")})
    so.apply(db, [{"standalone": "orphan-note-one"}])
    assert "orphan-note-one" not in report(db)
    assert links(db) == []
    with pytest.raises(ValueError):
        so.apply(db, [{"standalone": "orphan-note-one"}])
