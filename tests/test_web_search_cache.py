"""Tests for src/tools/web_search_cache.py -- SQLite cache of WebSearch results."""
import pytest

import src.tools.web_search_cache as wc


@pytest.fixture
def db(tmp_path):
    return tmp_path / "wsc.sqlite"


def test_ttl_rules():
    assert wc.ttl_for("nifty close today") == wc.LIVE_TTL_S
    assert wc.ttl_for("wall street 2026-11-19 selloff") == wc.LIVE_TTL_S
    assert wc.ttl_for("sqlite upsert syntax docs") == wc.DOC_TTL_S
    assert wc.ttl_for("history of the gold standard") == wc.DEFAULT_TTL_S


def test_hit_normalizes_query_and_counts(db):
    assert wc.store("  Gold   Standard ", {}, "result A", db=db, now=1000)
    hit = wc.lookup("gold standard", {}, db=db, now=1060)
    assert hit["result"] == "result A" and hit["age_s"] == 60 and hit["hits"] == 1
    assert wc.lookup("gold standard", {}, db=db, now=1061)["hits"] == 2


def test_expired_is_a_miss_but_row_kept(db):
    wc.store("gold standard", {}, "r", ttl_s=100, db=db, now=0)
    assert wc.lookup("gold standard", {}, db=db, now=101) is None
    assert wc.stats(db=db, now=101) == {"rows": 1, "fresh": 0, "stale": 1, "hits_served": 0}
    assert wc.search("gold", db=db)[0]["query"] == "gold standard"


def test_mode_and_domains_split_the_key(db):
    wc.store("q", {"mode": "extended"}, "deep", db=db, now=0)
    assert wc.lookup("q", {}, db=db, now=1) is None
    assert wc.lookup("q", {"mode": "extended"}, db=db, now=1)["result"] == "deep"
    assert wc.lookup("q", {"mode": "extended", "allowed_domains": ["a.com"]}, db=db, now=1) is None


def test_store_refreshes_and_resets_hits(db):
    wc.store("q", {}, "old", db=db, now=0)
    wc.lookup("q", {}, db=db, now=1)
    wc.store("q", {}, "new", db=db, now=2)
    hit = wc.lookup("q", {}, db=db, now=3)
    assert hit["result"] == "new" and hit["hits"] == 1


def test_result_text_shapes():
    assert wc.result_text("plain") == "plain"
    assert wc.result_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert wc.result_text({"content": [{"type": "text", "text": "x"}]}) == "x"
    assert wc.result_text(None) == ""


def test_empty_query_or_result_not_stored(db):
    assert not wc.store("", {}, "r", db=db)
    assert not wc.store("q", {}, "  ", db=db)


def test_purge(db):
    wc.store("old", {}, "r", db=db, now=0)
    wc.store("new", {}, "r", db=db, now=10 * 86400)
    assert wc.purge(5, db=db, now=10 * 86400) == 1
    assert wc.stats(db=db, now=10 * 86400)["rows"] == 1


def test_hooks_skip_test_sessions_and_never_raise(monkeypatch, db):
    monkeypatch.setattr(wc, "default_db", lambda: db)
    assert not wc.record_hook("test-123", {"query": "q"}, "r")
    assert wc.cached_reason("test-123", {"query": "q"}) is None
    assert wc.record_hook("real-1", {"query": "q"}, "r")
    assert "CACHED WebSearch result" in wc.cached_reason("real-1", {"query": "q"})
    monkeypatch.setattr(wc, "connect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert wc.cached_reason("real-1", {"query": "q"}) is None
    assert not wc.record_hook("real-1", {"query": "q"}, "r")


def test_dispatcher_serves_hit_then_misses_other_queries(monkeypatch, db):
    import sys
    sys.path.insert(0, "hooks")
    import dispatcher
    monkeypatch.setattr(wc, "default_db", lambda: db)
    ti = {"query": "gold standard history"}
    assert dispatcher._handle_pre_tool_use({"tool_name": "WebSearch", "session_id": "real-1", "tool_input": ti}) is None
    dispatcher._handle_post_tool_use({"tool_name": "WebSearch", "session_id": "real-1", "tool_input": ti,
                                      "tool_response": [{"type": "text", "text": "ANSWER"}]})
    out = dispatcher._handle_pre_tool_use({"tool_name": "WebSearch", "session_id": "real-1", "tool_input": ti})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ANSWER" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert dispatcher._handle_pre_tool_use(
        {"tool_name": "WebSearch", "session_id": "real-1", "tool_input": {"query": "other"}}) is None
