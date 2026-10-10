"""Router scoring, route_store persistence, RouteModelNode, report (task:9b311942)."""
from __future__ import annotations

import json

import pytest

from langchain_learning.nodes.route_model import RouteModelNode
from langchain_learning.router import TIER_MODEL, route
from src.db import route_store


@pytest.mark.parametrize("prompt,tier", [
    ("yes", "small"),
    ("ok go ahead", "small"),
    ("what does load_turn do?", "small"),
    ("add a flag to hooks/server.py", "medium"),
    ("Debug why the gate in hooks/gates.py and langchain_learning/nodes/gate_check.py "
     "denies calls and plan a refactor of tests/test_gates.py to avoid the race condition", "large"),
])
def test_tiers(prompt, tier):
    d = route(prompt)
    assert d.tier == tier, d
    assert d.model == TIER_MODEL[tier]


def test_empty_and_none_never_raise():
    assert route("").tier == "small"
    assert route(None).tier == "small"  # type: ignore[arg-type]


def test_prev_turn_tool_heavy_nudges_up():
    base = route("add a flag to hooks/server.py")
    assert route("add a flag to hooks/server.py", prev_tool_count=20).score == base.score + 1


@pytest.mark.parametrize("text,expected", [
    ("no, that's wrong", True), ("Nope", True), ("try again", True),
    ("now add tests", False), ("nothing else", False), ("", False),
])
def test_is_correction(text, expected):
    assert route_store.is_correction(text) is expected


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "r.sqlite"


def _rec(db, pid, sid="s1", tier="small"):
    assert route_store.record_decision(prompt_id=pid, session_id=sid, turn=1, prompt="hello there",
                                       tier=tier, model=TIER_MODEL[tier], effort="low", score=0,
                                       reasons=["x"], db=db)


def test_outcome_and_correction_hit_latest_decision_only(db):
    import sqlite3
    _rec(db, "p1"); _rec(db, "p2")
    assert route_store.record_outcome("s1", actual_model="claude-opus-5-5", effort="high",
                                      output_tokens=50, tool_calls=3, db=db)
    assert route_store.mark_correction("s1", db=db)
    rows = {r[0]: r for r in sqlite3.connect(db).execute(
        "SELECT prompt_id, actual_model, correction FROM route_decisions")}
    assert rows["p1"][1] is None and rows["p1"][2] == 0
    assert rows["p2"][1] == "claude-opus-5-5" and rows["p2"][2] == 1


def test_store_fails_open_on_bad_db(tmp_path):
    bad = tmp_path / "nodir" / "x.sqlite"
    assert route_store.mark_correction("s1", db=bad) is False
    assert route_store.record_outcome("s1", actual_model=None, effort=None,
                                      output_tokens=0, tool_calls=0, db=bad) is False


def test_read_turn_outcome_dedupes_usage_and_stops_at_user_prompt(tmp_path):
    t = tmp_path / "t.jsonl"
    lines = [
        {"message": {"role": "user", "content": "old prompt"}},
        {"message": {"role": "assistant", "id": "old", "model": "claude-haiku-5-5", "usage": {"output_tokens": 999}, "content": []}},
        {"message": {"role": "user", "content": "new prompt"}},
        {"message": {"role": "assistant", "id": "m1", "model": "claude-opus-5-5", "usage": {"output_tokens": 10},
                     "content": [{"type": "tool_use"}]}},
        {"message": {"role": "assistant", "id": "m1", "model": "claude-opus-5-5", "usage": {"output_tokens": 10},
                     "content": [{"type": "text"}]}},
        {"message": {"role": "user", "content": [{"type": "tool_result"}]}},
        {"message": {"role": "assistant", "id": "m2", "model": "claude-opus-5-5", "usage": {"output_tokens": 5},
                     "content": [{"type": "tool_use"}]}},
    ]
    t.write_text("\n".join(json.dumps(x) for x in lines))
    assert route_store.read_turn_outcome(str(t)) == {"model": "claude-opus-5-5", "output_tokens": 15, "tool_calls": 2}
    assert route_store.read_turn_outcome(str(tmp_path / "missing")) is None


def test_node_records_and_flags_correction(db, monkeypatch):
    import sqlite3
    monkeypatch.setattr("src.db.route_store.config", type("C", (), {"log_db": db})())
    node = RouteModelNode()
    base = {"session_id": "s1", "turn": 1, "session_prompt_ids": ["p1"], "session_tools": {"p1": []}}
    out = node({**base, "prompt": "what is this?", "prompt_id": "p1"})
    assert out["route_decision"]["tier"] == "small"
    node({**base, "prompt": "no, that's wrong", "prompt_id": "p2", "session_prompt_ids": ["p1", "p2"]})
    rows = dict(sqlite3.connect(db).execute("SELECT prompt_id, correction FROM route_decisions"))
    assert rows == {"p1": 1, "p2": 0}


def test_node_fails_open():
    assert RouteModelNode()(None) == {}  # type: ignore[arg-type]


def test_report_summarizes(db):
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
    from route_report import summarize
    _rec(db, "p1", tier="small")
    route_store.record_outcome("s1", actual_model="claude-opus-5-5", effort="high",
                               output_tokens=40, tool_calls=2, db=db)
    [row] = summarize(db)
    assert row["tier"] == "small" and row["ran_costlier"] == 1.0 and row["avg_output_tokens"] == 40
