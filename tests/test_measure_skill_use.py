"""scripts/measure_skill_use.py counts only skills loaded before the command (task:1b86af46)."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "measure_skill_use", Path(__file__).resolve().parents[1] / "scripts" / "measure_skill_use.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

SINCE = datetime(2026, 1, 10, tzinfo=timezone.utc)
UNTIL = datetime(2026, 1, 20, tzinfo=timezone.utc)


def _prompt(text="go"):
    return {"type": "user", "message": {"content": text}}


def _use(name, day=15, **inp):
    return {"type": "assistant", "timestamp": f"2026-01-{day:02d}T00:00:00.000Z",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _bash(command, day=15):
    return _use("Bash", day, command=command)


def _measure(tmp_path, *events):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(map(json.dumps, events)))
    return m.measure([str(path)], SINCE, UNTIL)


def test_skill_loaded_after_the_command_in_the_same_turn_is_bypassed(tmp_path):
    q = _measure(tmp_path, _prompt(), _bash("git commit -m x"), _use("Skill", skill="commit"),
                 _bash("git commit --amend"))
    assert q[("commit", "bypassed")]["n"] == 1
    assert q[("commit", "loaded")]["n"] == 1


def test_typed_command_and_skill_file_read_load_before_the_command(tmp_path):
    q = _measure(tmp_path, _prompt("<command-name>/commit</command-name>"), _bash("git commit -m x"),
                 _prompt(), _use("Read", file_path="/h/.claude/skills/add-memory/SKILL.md"),
                 _bash("mac memory add s"))
    assert q[("commit", "loaded")]["n"] == 1
    assert q[("memory add", "loaded")]["n"] == 1


def test_window_is_per_command_and_earlier_loads_still_count(tmp_path):
    q = _measure(tmp_path, _prompt(), _use("Skill", 1, skill="commit"), _bash("git commit -m old", 5),
                 _bash("git commit -m in", 10), _bash("git commit -m late", 20))
    assert list(q) == [("commit", "loaded")]
    assert q[("commit", "loaded")]["n"] == 1


def test_a_search_after_the_add_is_not_searched_first(tmp_path):
    late = _measure(tmp_path, _prompt(), _bash("mac memory add s && mac memory get s"),
                    _bash("mac memory search s"))
    assert late[("memory add", "bypassed")]["searched_first"] == 0
    early = _measure(tmp_path, _prompt(), _bash("mac memory search s"), _bash("mac memory add s"))
    assert early[("memory add", "bypassed")]["searched_first"] == 1
