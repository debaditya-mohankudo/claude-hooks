"""Group-level tool routing over ~/.claude/mcp-tools-domain.json (task:750242a5)."""
import json

import pytest

from langchain_learning import tool_groups as tg
from langchain_learning.tool_groups import format_groups, group_of, load_graph, route_groups


def _real_graph():
    # The real map is personal (~/.claude, not in the public repo); checks on it run only
    # where it exists. Structural behaviour is covered by the in-test _graph() fixtures.
    g = load_graph()
    if g is None:
        pytest.skip("~/.claude/mcp-tools-domain.json not present")
    return g


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
    graph = _real_graph()
    [r] = route_groups(_hints("vault__read"), graph)
    assert r["group"] == "local-mac/vault"
    assert r["category"] == "memory_knowledge"


def test_malformed_graph_shape_degrades_to_no_routing():
    # Valid JSON, wrong shape: must not raise (task:750242a5 grooming risk).
    hints = [{"tool_name": "vault__write"}]
    assert route_groups(hints, graph={"nodes": [{"id": "group:a:b"}], "edges": []}) == []
    assert route_groups(hints, graph={"edges": []}) == []


def test_failed_load_is_not_cached_and_edits_are_picked_up(tmp_path):
    p = tmp_path / "g.json"
    assert load_graph(p) is None
    p.write_text(json.dumps({"nodes": [], "edges": []}))
    assert load_graph(p) == {"nodes": [], "edges": []}
    p.write_text(json.dumps({"nodes": [1], "edges": []}))
    assert load_graph(p) == {"nodes": [1], "edges": []}


def test_routing_outcome_is_logged(caplog):
    import logging
    caplog.set_level(logging.INFO)
    tg._log.addHandler(caplog.handler)
    try:
        route_groups([{"tool_name": "vault__write"}, {"tool_name": "vault__read"}], graph=_graph())
        route_groups([{"tool_name": "nonexistent__x"}], graph=_graph())
    finally:
        tg._log.removeHandler(caplog.handler)
    msgs = [r.getMessage() for r in caplog.records if "[tool_groups]" in r.getMessage()]
    assert any("routed 2 hint(s) to 1 group(s): local-mac/vault(2)" in m for m in msgs)
    assert any("no group for 1 hint(s)" in m for m in msgs)


# --- graph-side keywords (task:21353636): measured, deliberately not wired into routing ---

def test_group_keywords_normalise_to_prompt_tokens():
    node = {"keywords": ["code_rag", "wyckoff-analyzer", "d10", "Vault"]}
    # tokenise() yields only [a-z]{3,}: separators split, digits-only fragments drop.
    assert tg.group_keywords(node) == {"code", "rag", "wyckoff", "analyzer", "vault"}


def test_group_without_keywords_is_unmatched_not_an_error():
    graph = {"nodes": [_g("local-mac", "vault", "memory_knowledge")], "edges": []}
    assert tg.group_keywords(graph["nodes"][0]) == set()
    assert tg.match_groups({"vault"}, graph) == {}


def test_match_groups_is_exact_token_not_substring():
    node = _g("local-mac", "vault", "memory_knowledge")
    node["keywords"] = ["tar"]
    graph = {"nodes": [node], "edges": []}
    assert tg.match_groups({"target"}, graph) == {}
    assert tg.match_groups({"tar"}, graph) == {"group:local-mac:vault": ["tar"]}


def test_match_groups_fails_soft_on_malformed_graph():
    assert tg.match_groups({"vault"}, {"nodes": [{"kind": "tool_group"}]}) == {}
    assert tg.match_groups({"vault"}, {}) == {}


def test_every_real_group_carries_keywords():
    graph = _real_graph()
    bare = [n["id"] for n in graph["nodes"] if n["kind"] == "tool_group" and not tg.group_keywords(n)]
    assert bare == []


def test_no_curated_keyword_is_silently_dropped_by_tokenise():
    # `time` and `now` were stopwords: a group keyworded only by them scored zero forever,
    # with no error anywhere (task:21353636).
    from langchain_learning.nodes._text_utils import tokenise
    lost = {n["id"]: [k for k in n["keywords"] if tokenise(k) != {k}]
            for n in _real_graph()["nodes"] if n["kind"] == "tool_group"}
    assert {g: k for g, k in lost.items() if k} == {}


# --- bounded_contexts (task:3fea9bfe): one description per MCP server, single source ---

def test_bounded_contexts_match_servers_exactly():
    g = _real_graph()
    servers = {n["id"].split(":", 1)[1] for n in g["nodes"] if n["kind"] == "server"}
    assert servers == set(g["bounded_contexts"])
    assert [k for k, v in g["bounded_contexts"].items() if len(v) < 40] == []


def test_every_tool_group_belongs_to_a_bounded_context():
    g = _real_graph()
    orphans = [n["id"] for n in g["nodes"] if n["kind"] == "tool_group"
               and n["id"].split(":")[1] not in g["bounded_contexts"]]
    assert orphans == []


def test_server_nodes_carry_no_second_copy_of_the_description():
    # Placeholder "MCP server / connector X" definitions were removed; the map is the source.
    g = _real_graph()
    assert [n["id"] for n in g["nodes"] if n["kind"] == "server" and "definition" in n] == []


# --- indexes (derived index -> its source): a rebuildable cache must name what it caches ---

def _index_groups(g):
    return [n["id"] for n in g["nodes"] if n["kind"] == "tool_group" and n["id"].endswith("_rag")]


def test_every_rag_group_indexes_exactly_one_existing_source():
    g = _real_graph()
    ids = {n["id"] for n in g["nodes"]}
    edges = [e for e in g["edges"] if e["relation"] == "indexes"]
    for gid in _index_groups(g):
        targets = [e["to"] for e in edges if e["from"] == gid]
        assert len(targets) == 1, f"{gid} indexes {targets}"
        assert targets[0] in ids, f"{gid} indexes missing node {targets[0]}"


def test_indexes_edges_only_leave_rag_groups():
    g = _real_graph()
    stray = [e["from"] for e in g["edges"] if e["relation"] == "indexes"
             and e["from"] not in _index_groups(g)]
    assert stray == []


def test_vault_rag_indexes_the_vault_group():
    g = _real_graph()
    assert {"from": "group:local-mac:vault_rag", "to": "group:local-mac:vault"}.items() <= next(
        e for e in g["edges"] if e["relation"] == "indexes" and e["from"] == "group:local-mac:vault_rag").items()


def test_every_edge_relation_is_declared_in_relation_types():
    g = _real_graph()
    assert {e["relation"] for e in g["edges"]} - set(g["relation_types"]) == set()


def test_indexes_edges_do_not_change_group_routing():
    # Routing reads only overlaps_with; adding indexes edges must not add alternatives.
    r = route_groups([{"tool_name": "vault_rag__query_vault"}], _real_graph())
    assert [(x["group"], x["alternatives"]) for x in r] == [("local-mac/vault_rag", [])]
