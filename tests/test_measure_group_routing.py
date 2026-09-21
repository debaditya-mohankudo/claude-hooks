"""scripts/measure_group_routing.py (task:21353636): turn grouping and the metric."""
import importlib.util
from pathlib import Path

from tests.test_tool_groups import _g

_spec = importlib.util.spec_from_file_location(
    "measure_group_routing", Path(__file__).resolve().parents[1] / "scripts" / "measure_group_routing.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_turn_is_prompt_plus_tools_until_next_prompt_within_a_session():
    rows = [("s1", 1, "prompt", "first"), ("s1", 2, "tool", "vault__read"),
            ("s2", 3, "tool", "orphan__tool"),           # no prompt yet in s2 -> dropped
            ("s1", 4, "prompt", "second"), ("s1", 5, "tool", "notes__add"),
            ("s2", 6, "prompt", "other"), ("s1", 7, "tool", "notes__list")]
    got = {p: t for _, p, t in m.turns(rows)}
    assert got == {"first": ["vault__read"], "second": ["notes__add", "notes__list"], "other": []}


class _Scorer:
    def __init__(self, hints):
        self.hints = hints

    def score(self, keywords, top_n=5):
        return [{"tool_name": t} for t in self.hints]


def _graph():
    vault = _g("local-mac", "vault", "memory_knowledge", "read")
    vault["keywords"] = ["vault"]
    notes = _g("local-mac", "notes", "memory_knowledge", "add")
    notes["keywords"] = ["apple"]
    return {"nodes": [vault, notes], "edges": []}


def test_evaluate_counts_db_and_graph_hits_against_eligible_tools_only():
    turns = [("s", "search my vault", ["vault__read", "Bash"])]   # Bash not in DB -> ineligible
    r = m.evaluate(turns, _graph(), _Scorer(["notes__add"]), {"vault__read", "notes__add"})
    assert (r["evaluated"], r["group_db"], r["group_graph"], r["group_either"]) == (1, 0, 1, 1)


def test_prompt_using_no_eligible_tool_is_not_evaluated():
    r = m.evaluate([("s", "hello", ["Bash"])], _graph(), _Scorer([]), {"vault__read"})
    assert (r["prompts"], r["evaluated"], r["db_no_hints"]) == (1, 0, 1)
