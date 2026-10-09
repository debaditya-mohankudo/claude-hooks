"""Behaviour of kitty/dashboard.py against a temp server_memory DB."""
import importlib.util
import sqlite3

import pytest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "dashboard", Path(__file__).parent.parent / "kitty" / "dashboard.py")
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)


@pytest.fixture(autouse=True)
def _no_real_note(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "NOTE", tmp_path / "absent-note.md")
    monkeypatch.setattr(dash, "PLAN", tmp_path / "absent-plan.json")
    monkeypatch.setattr(dash, "TURN", tmp_path / "absent-turn.json")


def _db(tmp_path, rows):
    p = tmp_path / "sm.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE server_memory (id INTEGER PRIMARY KEY, ts REAL, type TEXT, content TEXT, ref TEXT)")
    con.executemany("INSERT INTO server_memory (ts, type, content, ref) VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return p


def test_shows_latest_task_and_last_n_oldest_first(tmp_path):
    p = _db(tmp_path, [
        (1, "task", "old task", "aaa"), (2, "prompt", "p1", None), (3, "memories", "noise", None),
        (4, "tool", "t1", None), (5, "task", "new task", "bbb"), (6, "tool", "t2", None),
    ])
    out = dash.snapshot(p, 2, 80)
    assert out.splitlines()[0].startswith("★ new task") and "[bbb]" in out
    body = out.splitlines()[2:]
    assert len(body) == 2 and body[0].endswith("new task") and body[1].endswith("t2")
    assert "noise" not in out


def test_no_task_and_clipping(tmp_path):
    p = _db(tmp_path, [(1, "prompt", "x" * 200, None)])
    out = dash.snapshot(p, 5, 40)
    assert "no task activation" in out
    assert max(len(l) for l in out.splitlines()) <= 40 and "…" in out


def test_missing_db_fails_soft(tmp_path, capsys):
    out = dash.snapshot(tmp_path / "nope.sqlite", 5, 80)
    assert out.startswith("dashboard: cannot read")
    assert dash.main(["--once"]) in (0,)


def test_note_shown_above_feed_and_absent_note_ignored(tmp_path, monkeypatch):
    p = _db(tmp_path, [(1, "tool", "t1", None)])
    note = tmp_path / "note.md"
    monkeypatch.setattr(dash, "NOTE", note)
    assert "WAITING" not in dash.snapshot(p, 5, 80)
    note.write_text("PLAN: do x\n\nWAITING: your call on y\n")
    out = dash.snapshot(p, 5, 80).splitlines()
    assert out[0] == "PLAN: do x" and out[1] == "WAITING: your call on y"
    assert any(l.endswith("t1") for l in out)
