"""Tests for the memory audit log and the mac-area carry-over in LoadMemoriesNode."""
import pytest

from langchain_learning.nodes import _memory_audit as audit
from langchain_learning.nodes._memory_scoring import area_tokens
from langchain_learning.nodes.load_memories import LoadMemoriesNode


class _Stub:
    """Retriever stub: returns canned memories and records the tokens it was asked for."""
    def __init__(self, memories, trace=None):
        self.memories, self.last_trace, self.seen = memories, trace or [], []

    def retrieve(self, tokens, top_n=None):
        self.seen.append(set(tokens))
        return self.memories


def _mem(name, tags=""):
    return {"name": name, "tags": tags}


def _run(node, prompt, session="s1"):
    return node({"prompt": prompt, "session_id": session})


def test_area_tokens_from_mac_tags_most_frequent_first():
    mems = [_mem("a", "x mac-calendar calendar"), _mem("b", "mac-reminders"), _mem("c", "mac-calendar")]
    assert area_tokens(mems) == ["calendar", "reminders"]


def test_area_tokens_ignores_untagged():
    assert area_tokens([_mem("a", "plain tags")]) == []


def test_thin_prompt_borrows_previous_area():
    stub = _Stub([_mem("m", "mac-calendar")])
    node = LoadMemoriesNode(retriever=stub)
    _run(node, "calendar event reminder tomorrow morning")   # 5 tokens: not thin, no carry
    _run(node, "yes, go ahead with it")                                # thin: carries 'calendar'
    assert "calendar" not in stub.seen[0] or "mac" not in stub.seen[0]
    assert "calendar" in stub.seen[1]


def test_substantive_prompt_does_not_borrow():
    stub = _Stub([_mem("m", "mac-calendar")])
    node = LoadMemoriesNode(retriever=stub)
    _run(node, "first prompt about something")
    _run(node, "completely different long prompt about astrology charts today")
    assert "calendar" not in stub.seen[1]


def test_carry_is_per_session():
    stub = _Stub([_mem("m", "mac-calendar")])
    node = LoadMemoriesNode(retriever=stub)
    _run(node, "first prompt about something", session="a")
    _run(node, "yes, go ahead with it", session="b")
    assert "calendar" not in stub.seen[1]


def test_carry_window_forgets_old_prompts():
    stub = _Stub([_mem("m", "mac-calendar")])
    node = LoadMemoriesNode(retriever=stub)
    _run(node, "first prompt about something")
    stub.memories = [_mem("n", "plain")]
    for _ in range(3):
        _run(node, "other long prompt about unrelated matters entirely")
    _run(node, "yes, go ahead with it")
    assert "calendar" not in stub.seen[-1]


def test_audit_row_written_with_carried_and_candidates(tmp_path):
    stub = _Stub([_mem("m", "mac-calendar")], trace=[{"name": "m", "score": 1.0, "hits": ["calendar"]}])
    node = LoadMemoriesNode(retriever=stub)
    _run(node, "calendar event reminder tomorrow morning")
    _run(node, "yes, go ahead with it")
    rows = audit.recent()
    assert len(rows) == 2
    assert rows[0]["carried"] == ["calendar"] and rows[0]["injected"] == ["m"]
    assert rows[0]["candidates"][0]["hits"] == ["calendar"]


def test_audit_skips_empty_session_id():
    LoadMemoriesNode(retriever=_Stub([_mem("m")]))({"prompt": "hello there wide world"})
    assert audit.recent() == []


def test_audit_failure_never_breaks_retrieval(monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_DB", audit.Path("/nonexistent-dir/x/audit.sqlite"))
    out = _run(LoadMemoriesNode(retriever=_Stub([_mem("m")])), "hello there wide world")
    assert [m["name"] for m in out["memories"]] == ["m"]


def test_mark_merges_and_validates():
    audit.record("s", "p", {"t"}, [], [], ["a", "b"])
    rid = audit.recent()[0]["id"]
    audit.mark(rid, {"a": "useful"})
    audit.mark(rid, {"b": "noise"})
    assert audit.recent()[0]["verdict"] == {"a": "useful", "b": "noise"}
    with pytest.raises(ValueError):
        audit.mark(rid, {"a": "maybe"})
    with pytest.raises(ValueError):
        audit.mark(rid, {"zzz": "noise"})
    with pytest.raises(KeyError):
        audit.mark(9999, {"a": "noise"})


def test_report_precision_and_noise_terms():
    cand = [{"name": "a", "score": 1, "hits": ["idea"]}, {"name": "b", "score": 1, "hits": ["idea", "state"]}]
    audit.record("s", "p1", {"idea"}, ["calendar"], cand, ["a", "b"])
    audit.record("s", "p2", {"idea"}, [], cand, ["a"])
    rows = audit.recent()
    audit.mark(rows[1]["id"], {"a": "noise", "b": "useful"})
    audit.mark(rows[0]["id"], {"a": "useful"})
    r = audit.report()
    assert r["memories"]["a"] == {"useful": 1, "noise": 1, "precision": 0.5}
    assert r["noise_terms"][0] == ("idea", 1)
    assert r["carried_noise_rate"] == 0.5   # carried row p1: 1 noise of 2 verdicts


def test_recent_unmarked_only():
    audit.record("s", "p1", {"t"}, [], [], ["a"])
    audit.record("s", "p2", {"t"}, [], [], ["a"])
    audit.mark(audit.recent()[0]["id"], {"a": "useful"})
    assert [r["prompt"] for r in audit.recent(unmarked_only=True)] == ["p1"]


@pytest.mark.parametrize("prompt,reason", [
    ("close", "short"),
    ("start task f9e7b151", "short"),
    ('<agent-message from="a1">\nreport on the calendar work\n</agent-message>', "machine"),
    ('<cross-session-message from="uds:/x">please check the calendar</cross-session-message>', "machine"),
    ("<task-notification>\n<task-id>b1</task-id> finished the run", "machine"),
])
def test_skipped_prompt_injects_nothing_and_is_audited(prompt, reason):
    stub = _Stub([_mem("m")])
    out = _run(LoadMemoriesNode(retriever=stub), prompt)
    assert out["memories"] == [] and stub.seen == []
    row = audit.recent()[0]
    assert row["skipped"] == reason and row["injected"] == []
    assert audit.recent(unmarked_only=True) == []


def test_four_words_retrieve():
    out = _run(LoadMemoriesNode(retriever=_Stub([_mem("m")])), "commit and push it")
    assert [m["name"] for m in out["memories"]] == ["m"]


def test_skipped_prompts_do_not_age_out_carried_areas():
    stub = _Stub([_mem("m", "mac-calendar")])
    node = LoadMemoriesNode(retriever=stub)
    _run(node, "first prompt about something")
    for _ in range(5):
        _run(node, "ok")
    _run(node, "yes, go ahead with it")
    assert "calendar" in stub.seen[-1]


def test_skipped_prompt_clears_prompt_injected_set():
    from langchain_learning.nodes import _skill_memories as sm
    node = LoadMemoriesNode(retriever=_Stub([_mem("m")]))
    _run(node, "first prompt about something", session="skipclear")
    _run(node, "ok", session="skipclear")
    assert sm._PROMPT_INJECTED["skipclear"] == set()


def test_old_audit_db_gains_skipped_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE memory_audit (id INTEGER PRIMARY KEY, ts REAL, session_id TEXT, prompt TEXT, "
              "tokens TEXT, carried TEXT, candidates TEXT, injected TEXT, verdict TEXT)")
    c.commit()
    c.close()
    audit.record("s", "ok", set(), [], [], [], path=db, skipped="short")
    assert audit.recent(path=db)[0]["skipped"] == "short"


def test_mark_moves_mismatch_count_once_per_changed_verdict():
    import sqlite3
    from src.db.schema import MEMORIES_DDL

    con = sqlite3.connect(str(audit.MEMORY_DB))
    con.executescript(MEMORIES_DDL)
    con.execute("INSERT INTO memories (name, type, updated) VALUES ('a', 'project', '2020-01-01')")
    con.commit()
    audit.record("s1", "p1", {"x"}, [], [], ["a"])
    audit.record("s1", "p2", {"x"}, [], [], ["a"])
    second, first = [r["id"] for r in audit.recent()]

    def state():
        return con.execute("SELECT mismatch_count, updated FROM memories").fetchone()

    audit.mark(first, {"a": "noise"})
    audit.mark(first, {"a": "noise"})
    assert state() == (1, "2020-01-01")
    audit.mark(second, {"a": "noise"})
    assert state()[0] == 2
    audit.mark(second, {"a": "useful"})
    audit.mark(first, {"a": "useful"})
    assert state()[0] == 0  # floored, never negative
    con.close()
