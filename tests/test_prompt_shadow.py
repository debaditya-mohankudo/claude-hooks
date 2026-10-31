"""Tests for src/tools/prompt_shadow.py -- find_cli shadow run on whole prompts (task:444cc036)."""
import pytest

import src.tools.prompt_shadow as ps

SESSION = "abc12345-real"


def answer(top="mac git push", level="high"):
    def f(prompt):
        return {"seeds": [{"id": top, "score": 7.5}, {"id": "mac git commit", "score": 2.0}],
                "confidence": {"level": level, "coverage": 0.9, "gap": 5.5}}
    return f


@pytest.fixture
def db(tmp_path):
    return tmp_path / "hints.sqlite"


def rows(db):
    conn = ps.connect(db)
    try:
        return conn.execute("SELECT prompt, top, level, score, chosen FROM prompt_shadow ORDER BY id").fetchall()
    finally:
        conn.close()


def test_record_stores_the_answer_for_the_whole_prompt(db):
    assert ps.record(SESSION, "push it", db=db, answer=answer()) == 1
    assert rows(db) == [("push it", "mac git push", "high", 7.5, None)]


def test_prompt_is_cut_at_the_cap(db):
    ps.record(SESSION, "x " * 2500, db=db, answer=answer())
    assert len(rows(db)[0][0]) == ps.MAX_PROMPT


@pytest.mark.parametrize("session,prompt", [("test-1", "push it"), ("", "push it"),
                                            (SESSION, ""), (SESSION, "   ")])
def test_skipped_sessions_and_empty_prompts_store_nothing(db, session, prompt):
    assert ps.record(session, prompt, db=db, answer=answer()) is None
    assert rows(db) == []


@pytest.mark.parametrize("prompt", [
    "/task-grooming task:a5b86dd9", "<pasted_content id=\"x\">\nlog lines\n</pasted_content>",
    '<cross-session-message from="uds:/tmp/s.sock">\ndo a thing\n</cross-session-message>',
    "next?", "yes"])
def test_prompts_no_suggestion_fits_store_nothing(db, prompt):
    assert ps.record(SESSION, prompt, db=db, answer=answer()) is None
    assert rows(db) == []


def test_nothing_scored_stores_a_row_with_no_top(db):
    ps.record(SESSION, "hello there", db=db, answer=lambda p: {"seeds": [], "confidence": {"level": "none"}})
    assert rows(db)[0][1:3] == (None, "none")


def test_an_error_result_or_exception_never_raises_and_stores_nothing(db):
    assert ps.record(SESSION, "push it", db=db, answer=lambda p: {"error": {"code": "internal"}}) is None
    def boom(p):
        raise RuntimeError("x")
    assert ps.record(SESSION, "push it", db=db, answer=boom) is None
    assert rows(db) == []


def test_link_stores_the_first_mac_call_on_the_newest_unlinked_prompt(db):
    ps.record(SESSION, "first prompt", db=db, answer=answer(), now=100)
    ps.record(SESSION, "second prompt", db=db, answer=answer(), now=200)
    assert ps.link_calls(SESSION, ["mac git push", "mac git commit"], db=db, now=210) == "mac git push"
    assert [r[4] for r in rows(db)] == [None, "mac git push"]
    assert ps.link_calls(SESSION, ["mac git commit"], db=db, now=220) == "mac git commit"
    assert [r[4] for r in rows(db)] == ["mac git commit", "mac git push"]


def test_link_ignores_mac_cli_lookups_old_prompts_and_other_sessions(db):
    ps.record(SESSION, "old prompt", db=db, answer=answer(), now=100)
    assert ps.link_calls(SESSION, ["mac cli find"], db=db, now=110) is None
    assert ps.link_calls("other-session", ["mac git push"], db=db, now=110) is None
    assert ps.link_calls(SESSION, ["mac git push"], db=db, now=100 + ps.WINDOW_S + 1) is None
    assert rows(db)[0][4] is None


def test_stats_counts_hits_and_misses_by_level(db):
    ps.record(SESSION, "prompt a", db=db, answer=answer("mac git push", "high"), now=1)
    ps.link_calls(SESSION, ["mac git push"], db=db, now=2)
    ps.record(SESSION, "prompt b", db=db, answer=answer("mac music next", "high"), now=3)
    ps.link_calls(SESSION, ["mac git commit"], db=db, now=4)
    ps.record(SESSION, "prompt c", db=db, answer=answer("mac safari open", "low"), now=5)
    s = ps.stats(db=db, now=10)
    assert s["prompts"] == 3
    assert s["by_level"]["high"] == {"prompts": 2, "ran": 2, "hit": 1, "miss": 1}
    assert s["by_level"]["low"] == {"prompts": 1, "ran": 0, "hit": 0, "miss": 0}


def test_the_real_finder_answers_from_the_claude_hooks_interpreter(db):
    from hooks.paths import CLI_GRAPH_PATH
    if not CLI_GRAPH_PATH.exists():
        pytest.skip("cli-graph checkout not present")
    assert ps.record(SESSION, "push it", db=db) == 1
    assert rows(db)[0][1] == "mac git push"
