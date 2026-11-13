"""Skill-tagged memories: lookup, injection, nudge, tag merge (task:85801ef4)."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import memory as mem
from langchain_learning.nodes import _skill_memories as sm
from src.db.schema import MEMORIES_DDL, migrate_memory_db


@pytest.fixture
def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    path = Path(tmp.name)
    con = sqlite3.connect(path)
    con.executescript(MEMORIES_DDL)
    migrate_memory_db(con)
    con.close()
    sm._PROMPT_INJECTED.clear()
    sm._SKILLS_RUN.clear()
    with patch.object(mem, "MEMORY_DB", path):
        yield path
    path.unlink()


def _add(name, tags, **kw):
    return mem.handle_add(name=name, type="feedback", body=f"body {name}", tags=tags, **kw)


def _lookup(db, tag="skill-stock-tracker", **kw):
    con = sqlite3.connect(db)
    try:
        return sm.lookup(con, tag, **kw)
    finally:
        con.close()


def test_skill_tag_strips_plugin_prefix_and_slash():
    assert sm.skill_tag("stock-tracker") == "skill-stock-tracker"
    assert sm.skill_tag("anthropic-skills:pdf") == "skill-pdf"
    assert sm.skill_tag("/Commit") == "skill-commit"
    assert sm.skill_tag("") == ""


def test_exact_tag_match_not_substring(db):
    _add("a", "x, skill-stock-tracker")
    _add("b", "skill-stock-tracker-extra")
    _add("c", "unrelated")
    assert [m["name"] for m in _lookup(db)] == ["a"]


def test_cap_and_skip(db):
    for i in range(5):
        _add(f"m{i}", "skill-stock-tracker")
    assert len(_lookup(db)) == 3
    assert "m0" not in [m["name"] for m in _lookup(db, skip={"m0"}, cap=10)]


def test_superseded_excluded_and_conflicts_marked(db):
    _add("old", "skill-stock-tracker")
    _add("new", "skill-stock-tracker", links=[{"to": "old", "type": "supersedes"}])
    assert [m["name"] for m in _lookup(db)] == ["new"]
    _add("x", "skill-stock-tracker", links=[{"to": "new", "type": "contradicts"}])
    by = {m["name"]: m for m in _lookup(db)}
    assert by["x"]["conflicts_with"] == ["new"]


def test_no_tagged_memories_returns_none(db):
    with patch("langchain_learning.config.config") as cfg:
        cfg.memory_db = db
        with patch("src.encdb.open_db", lambda p, readonly=False: sqlite3.connect(p)):
            assert sm.inject_for_skill("s1", "stock-tracker") is None
    assert sm.last_skill_tag("s1") == "skill-stock-tracker"   # run still recorded


def test_inject_skips_prompt_injected_and_audits(db, tmp_path):
    _add("a", "skill-stock-tracker")
    _add("b", "skill-stock-tracker")
    sm.note_prompt_injected("s1", ["a"])
    audit = tmp_path / "audit.sqlite"
    from langchain_learning.nodes import _memory_audit as ma
    real = ma.record
    with patch("langchain_learning.config.config") as cfg, \
         patch("src.encdb.open_db", lambda p, readonly=False: sqlite3.connect(p)), \
         patch.object(ma, "record", lambda *a, **k: real(*a, path=audit, **k)), \
         patch("langchain_learning.nodes._memory_scoring.record_memory_hits"):
        cfg.memory_db = db
        out = sm.inject_for_skill("s1", "stock-tracker")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "### b" in text and "### a" not in text
    assert ma.recent(5, path=audit)[0]["injected"] == ["b"]


def test_nudge_only_after_skill_and_untagged(db):
    ids = ["mac memory add"]
    assert sm.tag_nudge("s1", ids, "mac memory add x") is None            # no skill ran
    sm.note_skill_run("s1", "skill-stock-tracker")
    assert "skill-stock-tracker" in sm.tag_nudge("s1", ids, "mac memory add x")["hookSpecificOutput"]["additionalContext"]
    assert sm.tag_nudge("s1", ids, "mac memory add x --tags skill-stock-tracker") is None
    assert sm.tag_nudge("s1", ["mac memory search"], "mac memory search q") is None
    assert sm.tag_nudge("s2", ids, "mac memory add x") is None            # other session


def test_readd_keeps_skill_tags_but_replaces_others(db):
    _add("m", "keep-me, skill-stock-tracker")
    _add("m", "fresh")
    tags = mem.handle_get("m")["tags"]
    assert "skill-stock-tracker" in tags and "fresh" in tags and "keep-me" not in tags


def test_readd_does_not_duplicate_skill_tag(db):
    _add("m", "skill-stock-tracker")
    _add("m", "skill-stock-tracker, other")
    assert mem.handle_get("m")["tags"].count("skill-stock-tracker") == 1


def test_batch_readd_keeps_skill_tags(db):
    _add("m", "skill-stock-tracker")
    mem.handle_add_batch([{"name": "m", "type": "feedback", "body": "b", "tags": "fresh"}])
    assert "skill-stock-tracker" in mem.handle_get("m")["tags"]


def test_dispatcher_routes_skill_and_memory_nudge(db):
    from hooks import dispatcher
    with patch.object(sm, "inject_for_skill", return_value={"x": 1}) as inj:
        out = dispatcher._handle_post_tool_use(
            {"tool_name": "Skill", "session_id": "s9", "tool_input": {"skill": "stock-tracker"}})
    assert out == {"x": 1}
    inj.assert_called_once_with("s9", "stock-tracker")
    sm.note_skill_run("s9", "skill-stock-tracker")
    out = dispatcher._handle_post_tool_use(
        {"tool_name": "Bash", "session_id": "s9", "tool_input": {"command": "mac memory add foo --type feedback"}})
    assert "skill-stock-tracker" in out["hookSpecificOutput"]["additionalContext"]
