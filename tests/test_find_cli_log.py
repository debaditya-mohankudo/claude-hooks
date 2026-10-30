"""Tests for src/tools/find_cli_log.py -- find_cli hit rate from real use (task:cf5d016d)."""
import json

import pytest

import src.tools.find_cli_log as fl

SESSION = "abc12345-real"


def result(seeds, neighbours=(), query="list reminders"):
    return {"query": query,
            "seeds": [{"id": s, "score": 1.0} for s in seeds],
            "neighbours": [{"id": n, "via": "in_domain"} for n in neighbours]}


def as_blocks(res):
    """The shape Claude Code hands PostToolUse for an MCP result."""
    return [{"type": "text", "text": json.dumps(res)}]


@pytest.fixture
def db(tmp_path):
    return tmp_path / "hints.sqlite"


def rows(db):
    conn = fl.connect(db)
    try:
        return conn.execute(
            "SELECT query, seeds, chosen, chosen_rank, outcome FROM find_cli_queries ORDER BY id").fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("shape", [
    lambda r: r,
    as_blocks,
    lambda r: {"content": as_blocks(r)},
    lambda r: {"raw": json.dumps(r)},
    lambda r: json.dumps(r),
])
def test_parse_result_accepts_every_response_shape(shape):
    res = result(["mac reminders list"])
    assert fl.parse_result(shape(res)) == res


def test_parse_result_rejects_non_find_cli_payloads():
    assert fl.parse_result({"error": "boom"}) is None
    assert fl.parse_result([{"type": "text", "text": "not json"}]) is None
    assert fl.parse_result(None) is None


def test_record_then_link_seed_hit_with_rank(db):
    res = result(["mac reminders lists", "mac reminders list"], ["mac reminders create"])
    assert fl.record_query(SESSION, {"task": "list reminders"}, as_blocks(res), db=db, now=100.0)
    assert fl.link_calls(SESSION, ["mac reminders list"], db=db, now=110.0) == "seed"
    assert rows(db) == [("list reminders", '["mac reminders lists", "mac reminders list"]',
                         "mac reminders list", 2, "seed")]


def test_neighbour_and_miss_outcomes(db):
    res = result(["mac reminders list"], ["mac reminders create"])
    fl.record_query(SESSION, {}, res, db=db, now=100.0)
    assert fl.link_calls(SESSION, ["mac reminders create"], db=db, now=101.0) == "neighbour"
    fl.record_query(SESSION, {}, res, db=db, now=200.0)
    assert fl.link_calls(SESSION, ["mac calendar list_events"], db=db, now=201.0) == "miss"
    assert [r[4] for r in rows(db)] == ["neighbour", "miss"]


def test_mac_cli_find_is_not_judged_a_miss(db):
    fl.record_query(SESSION, {}, result(["mac reminders list"]), db=db, now=100.0)
    assert fl.link_calls(SESSION, ["mac cli find"], db=db, now=101.0) is None
    assert rows(db)[0][4] is None
    assert fl.link_calls(SESSION, ["mac cli find", "mac reminders list"], db=db, now=102.0) == "seed"


def test_several_calls_in_one_line_are_judged_by_the_best_match(db):
    fl.record_query(SESSION, {}, result(["mac reminders list"], ["mac calendar list_events"]),
                    db=db, now=100.0)
    outcome = fl.link_calls(SESSION, ["mac safari open", "mac calendar list_events", "mac reminders list"],
                            db=db, now=101.0)
    assert outcome == "seed"
    assert rows(db)[0][2] == "mac reminders list"


def test_only_the_first_mac_call_after_a_query_links(db):
    fl.record_query(SESSION, {}, result(["mac reminders list"]), db=db, now=100.0)
    assert fl.link_calls(SESSION, ["mac reminders list"], db=db, now=101.0) == "seed"
    assert fl.link_calls(SESSION, ["mac calendar list_events"], db=db, now=102.0) is None
    assert rows(db)[0][4] == "seed"


def test_no_query_in_session_or_other_session_or_too_old_links_nothing(db):
    assert fl.link_calls(SESSION, ["mac reminders list"], db=db, now=100.0) is None
    fl.record_query("other-session", {}, result(["mac reminders list"]), db=db, now=100.0)
    assert fl.link_calls(SESSION, ["mac reminders list"], db=db, now=101.0) is None
    fl.record_query(SESSION, {}, result(["mac reminders list"]), db=db, now=100.0)
    assert fl.link_calls(SESSION, ["mac reminders list"], db=db, now=100.0 + fl.WINDOW_S + 1) is None
    assert [r[4] for r in rows(db)] == [None, None]


def test_newest_pending_query_wins(db):
    fl.record_query(SESSION, {}, result(["mac notes add"], query="old"), db=db, now=100.0)
    fl.record_query(SESSION, {}, result(["mac reminders list"], query="new"), db=db, now=200.0)
    assert fl.link_calls(SESSION, ["mac reminders list"], db=db, now=201.0) == "seed"
    assert [(r[0], r[4]) for r in rows(db)] == [("old", None), ("new", "seed")]


def test_test_sessions_are_never_written(db):
    for sid in ("test-1", "pytest-x", "api-test-y", ""):
        assert fl.record_query(sid, {}, result(["mac reminders list"]), db=db) is None
    assert rows(db) == []


def test_hook_entry_points_never_raise(tmp_path):
    bad = tmp_path / "missing-dir" / "hints.sqlite"
    assert fl.record_query(SESSION, {}, result(["mac reminders list"]), db=bad) is None
    assert fl.link_calls(SESSION, ["mac reminders list"], db=bad) is None


def test_stats_rates_unused_and_recent_misses(db):
    fl.record_query(SESSION, {}, result(["mac reminders list"]), db=db, now=100.0)
    fl.link_calls(SESSION, ["mac reminders list"], db=db, now=101.0)              # seed, rank 1
    fl.record_query(SESSION, {}, result(["mac a x", "mac reminders list"]), db=db, now=200.0)
    fl.link_calls(SESSION, ["mac reminders list"], db=db, now=201.0)              # seed, rank 2
    fl.record_query(SESSION, {}, result(["mac a x"], ["mac a y"]), db=db, now=300.0)
    fl.link_calls(SESSION, ["mac a y"], db=db, now=301.0)                          # neighbour
    fl.record_query(SESSION, {}, result(["mac a x"], query="lost"), db=db, now=400.0)
    fl.link_calls(SESSION, ["mac b z"], db=db, now=401.0)                          # miss
    fl.record_query(SESSION, {}, result(["mac a x"]), db=db, now=500.0)           # unused
    s = fl.stats(db=db, now=600.0)
    assert (s["queries"], s["linked"], s["unused"]) == (5, 4, 1)
    assert (s["seed"], s["neighbour"], s["miss"]) == (2, 1, 1)
    assert (s["top1_rate"], s["seed_rate"], s["subgraph_rate"]) == (0.25, 0.5, 0.75)
    assert s["recent_misses"][0]["query"] == "lost"
    assert s["recent_misses"][0]["chosen"] == "mac b z"


def test_stats_on_empty_db(db):
    s = fl.stats(db=db)
    assert s["linked"] == 0 and s["seed_rate"] is None and s["no_fit"] == 0


# task:9d42faae: a correct "nothing fits" is labelled no_fit and is not a miss.

def test_no_fit_label_replaces_a_miss_and_leaves_the_rates(db):
    fl.record_query(SESSION, {}, result(["mac reminders list"]), db=db, now=100.0)
    fl.link_calls(SESSION, ["mac reminders list"], db=db, now=101.0)              # seed
    miss = fl.record_query(SESSION, {}, result(["mac a x"], query="fred"), db=db, now=200.0)
    fl.link_calls(SESSION, ["mac b z"], db=db, now=201.0)                          # miss
    assert fl.stats(db=db, now=300.0)["seed_rate"] == 0.5
    assert fl.label(miss, "no_fit", db=db) == "miss"
    s = fl.stats(db=db, now=300.0)
    assert (s["queries"], s["linked"], s["no_fit"], s["miss"]) == (2, 1, 1, 0)
    assert s["seed_rate"] == 1.0 and s["recent_misses"] == []


def test_no_fit_label_on_an_unused_row_stops_a_later_call_linking_to_it(db):
    rid = fl.record_query(SESSION, {}, result(["mac a x"]), db=db, now=100.0)
    assert fl.label(rid, "no_fit", db=db) is None
    assert fl.link_calls(SESSION, ["mac a x"], db=db, now=101.0) is None
    assert rows(db)[0][4] == "no_fit"


def test_label_refuses_a_seed_row_an_unknown_label_and_a_missing_row(db):
    rid = fl.record_query(SESSION, {}, result(["mac reminders list"]), db=db, now=100.0)
    fl.link_calls(SESSION, ["mac reminders list"], db=db, now=101.0)
    with pytest.raises(ValueError, match="seed"):
        fl.label(rid, "no_fit", db=db)
    with pytest.raises(ValueError, match="unknown label"):
        fl.label(rid, "miss", db=db)
    with pytest.raises(ValueError, match="no find_cli query"):
        fl.label(999, "no_fit", db=db)
    assert rows(db)[0][4] == "seed"


def test_label_is_idempotent(db):
    rid = fl.record_query(SESSION, {}, result(["mac a x"]), db=db, now=100.0)
    fl.label(rid, "no_fit", db=db)
    assert fl.label(rid, "no_fit", db=db) == "no_fit"
