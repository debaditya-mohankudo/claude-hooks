"""hooks/session_store.py — the per-session dict that replaced the LangGraph checkpointer (task:31fb711d)."""
import pytest

from hooks import session_store as store


@pytest.fixture(autouse=True)
def _fresh():
    store.reset()
    yield
    store.reset()


def test_turn_counter_increments_and_mints_a_new_prompt_id_each_turn():
    first = store.begin_turn("s", "one")
    second = store.begin_turn("s", "two")
    assert (first["turn"], second["turn"]) == (1, 2)
    assert second["prompt"] == "two"
    assert first["prompt_id"] and first["prompt_id"] != second["prompt_id"]


def test_chime_fires_once_per_turn_and_again_after_the_next_begin_turn():
    store.begin_turn("s", "p")
    assert [store.mark_stop("s"), store.mark_stop("s")] == [True, False]
    store.begin_turn("s", "next")
    assert store.mark_stop("s") is True


def test_stop_without_a_prior_turn_still_chimes_once():
    assert [store.mark_stop("s"), store.mark_stop("s")] == [True, False]


def test_end_evicts_and_reports_whether_an_entry_existed():
    store.begin_turn("s", "p")
    assert store.end("s") is True
    assert not store.exists("s")
    assert store.end("s") is False


def test_start_reports_new_then_resumed_and_keeps_state():
    assert store.start("s") is True
    store.begin_turn("s", "p")
    assert store.start("s") is False
    assert store.get("s")["turn"] == 1


def test_cap_evicts_the_oldest_touched_session(monkeypatch):
    monkeypatch.setattr(store, "_MAX_SESSIONS", 3)
    for sid in ("a", "b", "c"):
        store.begin_turn(sid, "p")
    store.begin_turn("a", "p")      # a is now the most recently touched
    store.begin_turn("d", "p")      # pushes out b, the oldest
    assert [store.exists(s) for s in "abcd"] == [True, False, True, True]


def test_current_skips_test_prefixed_sessions():
    store.begin_turn("real", "p")
    store.begin_turn(f"{store.TEST_SESSION_PREFIX}x", "p")
    assert store.current() == {"session_id": "real", "turn": 1}


def test_current_is_empty_when_only_test_sessions_exist():
    store.begin_turn(f"{store.TEST_SESSION_PREFIX}x", "p")
    assert store.current() == {}


def test_get_does_not_create_an_entry_and_returns_a_copy():
    assert store.get("ghost")["turn"] == 0
    assert not store.exists("ghost")
    store.begin_turn("s", "p")
    store.get("s")["turn"] = 99
    assert store.get("s")["turn"] == 1


# --- dispatcher wiring -----------------------------------------------------

def test_dispatcher_stop_chimes_once_per_turn(monkeypatch):
    import hooks.dispatcher as d
    from hooks import stop_effects
    chimes = []
    monkeypatch.setattr(stop_effects, "play_completion_sound", lambda sid="": chimes.append(sid))
    monkeypatch.setattr(stop_effects, "live_claude_sessions", lambda: [])
    store.begin_turn("sess", "p")
    d._handle_stop({"session_id": "sess"})
    d._handle_stop({"session_id": "sess"})
    assert chimes == ["sess"]
    store.begin_turn("sess", "again")
    d._handle_stop({"session_id": "sess"})
    assert chimes == ["sess", "sess"]


def test_dispatcher_denies_git_commit_without_a_task_id():
    import hooks.dispatcher as d
    store.begin_turn("sess", "p")
    out = d._handle_pre_tool_use({"tool_name": "Bash", "session_id": "sess",
                                  "tool_input": {"command": 'git commit -m "no task here"'}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_dispatcher_allows_git_commit_citing_a_task_id():
    import hooks.dispatcher as d
    store.begin_turn("sess", "p")
    out = d._handle_pre_tool_use({"tool_name": "Bash", "session_id": "sess",
                                  "tool_input": {"command": 'git commit -m "fix\n\ntask:abcdef12"'}})
    assert not out or out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
