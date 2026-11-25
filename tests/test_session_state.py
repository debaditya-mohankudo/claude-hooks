"""Tests for hooks/session_state.py — get_current_session over hooks/session_store.py.

get_active_session was removed (task:8529435a): task-framework owns the active
task (tasks__active). The MemorySaver-era _latest_checkpoint_tuple tests went with
the graph (task:31fb711d); the "most recent session" rule is now session_store.current().
"""
from unittest.mock import patch

import pytest

from hooks import session_store
from hooks.session_state import get_current_session


@pytest.fixture(autouse=True)
def _fresh():
    session_store.reset()
    yield
    session_store.reset()


def test_returns_session_id_and_turn():
    session_store.begin_turn("sess-abc", "p1")
    session_store.begin_turn("sess-abc", "p2")
    assert get_current_session() == {"session_id": "sess-abc", "turn": 2}


def test_no_session_returns_empty():
    assert get_current_session() == {}


def test_result_has_no_task_fields():
    session_store.begin_turn("sess-xyz", "p")
    assert set(get_current_session()) == {"session_id", "turn"}


def test_store_error_fails_open():
    with patch("hooks.session_store.current", side_effect=RuntimeError("boom")):
        assert get_current_session() == {}


def test_most_recently_touched_session_wins_not_first_inserted():
    """The bug behind task:a4531510: a long-lived first-inserted session must not
    shadow newer ones."""
    session_store.begin_turn("aaa-old", "p")
    session_store.begin_turn("zzz-new", "p")
    assert get_current_session()["session_id"] == "zzz-new"
    session_store.begin_turn("aaa-old", "p")
    assert get_current_session()["session_id"] == "aaa-old"
