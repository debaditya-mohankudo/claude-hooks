"""Group-level tool routing over ontology/mcp-tools-domain.json (task:750242a5)."""
import json

from langchain_learning import tool_groups as tg
from langchain_learning.tool_groups import format_groups, group_of, load_graph, route_groups


def _g(server, name, category, tools=""):
    return {"id": f"group:{server}:{name}", "label": f"{server}/{name}", "kind": "tool_group",
            "category": category, "tools": tools}


def _graph():
    return {
        "nodes": [
            _g("local-mac", "vault", "memory_knowledge", "read, write, append"),
            _g("local-mac", "notes", "memory_knowledge", "add, read"),
            _g("local-mac", "code_rag", "memory_knowledge"),
            _g("claude-hooks", "code_rag", "memory_knowledge"),
            _g("wyckoff-analyzer", "wyckoff-analyzer", "market_analysis",
               "db_init_state, update_run, momentum_* (universe, rebalance)"),
            {"id": "market_analysis", "kind": "category"},
        ],
        "edges": [
            {"from": "group:local-mac:vault", "to": "group:local-mac:notes", "relation": "overlaps_with"},
            {"from": "group:local-mac:vault", "to": "market_analysis", "relation": "in_category"},
        ],
    }


def _hints(*names):
    return [{"tool_name": n} for n in names]


def test_hits_lift_to_group_with_category_siblings_and_overlaps():
    [r] = route_groups(_hints("vault__write", "vault__read"), _graph())
    assert r["group"] == "local-mac/vault"
    assert r["category"] == "memory_knowledge"
    assert r["matched"] == ["vault__write", "vault__read"]
    assert r["tools"] == "read, write, append"
    assert r["alternatives"] == ["local-mac/notes"]


def test_groups_rank_by_hit_count_then_best_position():
    hints = _hints("notes__add", "vault__read", "vault__write")
    ranked = [r["group"] for r in route_groups(hints, _graph())]
    assert ranked == ["local-mac/vault", "local-mac/notes"]
    tie = [r["group"] for r in route_groups(_hints("notes__add", "vault__read"), _graph())]
    assert tie == ["local-mac/notes", "local-mac/vault"]


def test_top_groups_caps_result():
    hints = _hints("vault__read", "notes__add", "update_run")
    assert len(route_groups(hints, _graph(), top_groups=2)) == 2


def test_ambiguous_group_name_is_skipped():
    graph = _graph()
    assert route_groups(_hints("code_rag__query"), graph) == []
    groups, _, by_name = tg._index(graph)
    assert group_of("code_rag__query", groups, by_name) is None


def test_unknown_group_is_skipped_without_error():
    assert route_groups(_hints("nonesuch__thing", "Bash"), _graph()) == []


def test_bare_names_and_wildcards_resolve_to_wyckoff_group():
    groups, _, by_name = tg._index(_graph())
    target = "group:wyckoff-analyzer:wyckoff-analyzer"
    assert group_of("update_run", groups, by_name) == target
    assert group_of("momentum_universe", groups, by_name) == target
    assert group_of("unrelated_tool", groups, by_name) is None


def test_empty_hints_and_missing_graph_yield_nothing(tmp_path):
    assert route_groups([], _graph()) == []
    assert route_groups(_hints("vault__read"), {}) == []
    assert load_graph(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_graph(bad) is None


def test_format_groups_renders_section_and_truncates_tools():
    routes = [{"group": "local-mac/vault", "category": "memory_knowledge", "matched": ["vault__read"],
               "tools": "x" * 300, "alternatives": ["local-mac/notes"]}]
    lines = format_groups(routes, max_tools=20)
    assert lines[0] == "## Suggested tool groups"
    assert "`local-mac/vault` [memory_knowledge]" in lines[1]
    assert "hit: vault__read" in lines[1]
    assert "..." in lines[1] and "overlaps: local-mac/notes" in lines[1]
    assert format_groups([]) == []


def test_real_graph_routes_a_known_tool():
    graph = json.loads(tg.GRAPH_PATH.read_text())
    [r] = route_groups(_hints("vault__read"), graph)
    assert r["group"] == "local-mac/vault"
    assert r["category"] == "memory_knowledge"
