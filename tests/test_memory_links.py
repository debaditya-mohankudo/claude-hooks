"""Contract tests for typed memory links (task:5f08a3b8)."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import memory as mem
from src.db.schema import MEMORIES_DDL, migrate_memory_db


@pytest.fixture
def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    path = Path(tmp.name)
    con = sqlite3.connect(path)
    con.executescript(MEMORIES_DDL)
    migrate_memory_db(con)
    con.close()
    with patch.object(mem, "MEMORY_DB", path):
        yield path
    path.unlink()


def _add(name, **kw):
    return mem.handle_add(name=name, type="project", body="b", **kw)


def _edges(db):
    with sqlite3.connect(db) as con:
        return con.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]


def test_add_with_typed_link_reads_back_from_both_ends(db):
    _add("old")
    assert _add("new", links=[{"to": "old", "type": "supersedes"}])["ok"]
    assert mem.handle_get("new")["links"] == [{"type": "supersedes", "name": "old", "direction": "out"}]
    assert mem.handle_get("old")["links"] == [{"type": "supersedes", "name": "new", "direction": "in"}]


def test_symmetric_link_stored_once_visible_from_either_end(db):
    _add("a")
    _add("b", links=["a"])  # bare slug -> relates_to
    assert _edges(db) == 1
    assert mem.handle_get("a")["links"] == [{"type": "relates_to", "name": "b", "direction": "both"}]
    assert mem.handle_get("b")["links"] == [{"type": "relates_to", "name": "a", "direction": "both"}]
    _add("a", links=["b"])  # reverse declaration collapses onto the same edge
    assert _edges(db) == 1


def test_unknown_type_rejected_and_nothing_written(db):
    _add("t")
    r = _add("n", links=[{"to": "t", "type": "bogus"}])
    assert "unknown link type" in r["error"]
    assert "error" in mem.handle_get("n")


def test_missing_target_rejected(db):
    r = _add("n", links=["ghost"])
    assert "ghost" in r["error"]
    assert "error" in mem.handle_get("n")


def test_db_rejects_bad_edges_even_with_fk_pragma_off(db):
    _add("a"); _add("b")
    with sqlite3.connect(db) as con:  # raw connection: FKs off, like the sqlite3 CLI
        for sql in (
            "INSERT INTO memory_links (src_id,dst_id,type) VALUES (1,2,'bogus')",
            "INSERT INTO memory_links (src_id,dst_id,type) VALUES (1,999,'relates_to')",
            "INSERT INTO memory_links (src_id,dst_id,type) VALUES (2,1,'relates_to')",  # non-canonical symmetric
        ):
            with pytest.raises(sqlite3.DatabaseError):
                con.execute(sql)


def test_delete_leaves_no_dangling_edge(db):
    _add("a"); _add("b", links=["a"])
    mem.handle_delete("a")
    assert _edges(db) == 0
    assert mem.handle_get("b")["links"] == []


def test_readd_keeps_edges(db):
    _add("a"); _add("b", links=["a"])
    _add("a")  # upsert, no links param
    assert _edges(db) == 1


def test_batch_links_can_target_same_batch_and_bad_one_is_isolated(db):
    r = mem.handle_add_batch([
        {"name": "x", "type": "project", "body": "b"},
        {"name": "y", "type": "project", "body": "b", "links": [{"to": "x", "type": "extends"}]},
        {"name": "z", "type": "project", "body": "b", "links": ["ghost"]},
    ])
    assert r["count"] == 2
    assert "ghost" in r["results"][2]["error"]
    assert mem.handle_get("x")["links"][0]["direction"] == "in"
    assert "error" in mem.handle_get("z")


def test_search_includes_links(db):
    _add("a"); _add("b", links=["a"])
    res = mem.handle_search("b")["results"]
    assert any(x["name"] == "b" and x["links"] for x in res)


def test_self_link_rejected(db):
    assert "itself" in _add("a", links=["a"])["error"]


def test_legacy_db_without_links_table_still_reads(db):
    with sqlite3.connect(db) as con:
        con.executescript("DROP TRIGGER memory_links_cascade; DROP TABLE memory_links;")
        con.execute("INSERT INTO memories (name,type,body) VALUES ('old','user','b')")
    assert mem.handle_get("old")["links"] == []


def test_backfill_copies_valid_skips_dangling_and_is_idempotent(db, tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from backfill_memory_links import backfill
    with sqlite3.connect(db) as con:
        con.executemany("INSERT INTO memories (name,type,body,related) VALUES (?,?,?,?)", [
            ("a", "user", "b", "b,ghost"), ("b", "user", "b", "a"), ("c", "user", "b", "a")])
    report = tmp_path / "dangling.tsv"
    r1 = backfill(db, report)
    assert (r1["added"], r1["dangling"]) == (2, 1)      # a-b (reciprocal, once) and a-c
    assert "ghost" in report.read_text()
    assert backfill(db, report)["added"] == 0           # rerun adds nothing
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT related FROM memories WHERE name='a'").fetchone()[0] == "b,ghost"
    assert [l["name"] for l in mem.handle_get("b")["links"]] == ["a"]


def test_related_mirrored_into_links_and_unresolved_reported(db):
    _add("a")
    r = _add("b", related="a, ghost, b")
    assert r["ok"] and r["unresolved_related"] == ["ghost", "b"]
    assert _edges(db) == 1
    assert mem.handle_get("a")["links"] == [{"type": "relates_to", "name": "b", "direction": "both"}]
    _add("b", related="a")  # re-add is idempotent
    assert _edges(db) == 1


def test_batch_related_may_target_same_batch(db):
    r = mem.handle_add_batch([
        {"name": "x", "type": "project", "body": "b", "related": "y"},
        {"name": "y", "type": "project", "body": "b", "related": "nope"},
    ])
    assert _edges(db) == 1
    res = {x["name"]: x for x in r["results"]}
    assert "unresolved_related" not in res["x"]
    assert res["y"]["unresolved_related"] == ["nope"]
