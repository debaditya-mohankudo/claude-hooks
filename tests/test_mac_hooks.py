"""Hooks treat a `mac` command the same whether it ran through Bash or MCP (task:0aa96180)."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from core import mac_command as mc

ROOT = Path(__file__).resolve().parents[1]
IDS = ["mac vault write", "mac vault move", "mac mail read", "mac imessage send",
       "mac wyckoff db_init_state", "mac astrology aq_planets", "mac sysml get_complexity"]


@pytest.fixture(autouse=True)
def graph(tmp_path, monkeypatch):
    path = tmp_path / "cli_graph.json"
    path.write_text(json.dumps({"nodes": [{"kind": "command", "id": i} for i in IDS]}))
    monkeypatch.setattr(mc, "CLI_GRAPH_PATH", path)
    mc.known_ids.cache_clear()
    yield
    mc.known_ids.cache_clear()


# --- PostToolUse --------------------------------------------------------------

@pytest.fixture
def ptu(monkeypatch):
    """Run the PostToolUse handler, capturing vault reindexes and hint-learning calls."""
    import hooks.dispatcher as d
    import langchain_learning.session_graph as sg
    import src.tools.vault_index as vi
    seen = {"reindex": [], "learn": []}
    monkeypatch.setattr(vi, "reindex_for_command", lambda cid, inp: seen["reindex"].append((cid, inp)))
    monkeypatch.setattr(sg, "run_post_tool", lambda **kw: seen["learn"].append(kw) or None)
    monkeypatch.setattr(d, "_maybe_context_size_nudge", lambda *a: None)

    def run(tool_name, tool_input, tool_response=None):
        d._handle_post_tool_use({"tool_name": tool_name, "tool_input": tool_input, "session_id": "pytest-x",
                                 "tool_response": tool_response or {}})
        return seen
    return run


def test_a_bash_vault_write_reindexes_the_note_like_the_mcp_write(ptu):
    seen = ptu("Bash", {"command": 'mac vault write "Notes/a b.md" "body"'})
    assert seen["reindex"] == [("mac vault write", {"path": "Notes/a b.md"})]


def test_a_bash_vault_move_reindexes_both_paths(ptu):
    seen = ptu("Bash", {"command": "mac vault move --json a.md b/c.md"})
    assert seen["reindex"] == [("mac vault move", {"path": "a.md", "to": "b/c.md"})]


def test_an_mcp_vault_write_reaches_the_same_reindex(ptu):
    seen = ptu("mcp__local-mac__vault__write", {"path": "x.md", "content": "y"})
    assert seen["reindex"] == [("mac vault write", {"path": "x.md", "content": "y"})]


def test_text_naming_a_vault_verb_triggers_nothing(ptu):
    seen = ptu("Bash", {"command": 'grep -rn "mac vault write" hooks/'})
    assert seen == {"reindex": [], "learn": []}


def test_hints_learn_mac_ids_from_both_forms_with_the_parsed_result(ptu):
    seen = ptu("Bash", {"command": "mac mail read --limit 1"}, {"stdout": '{"messages": []}', "stderr": ""})
    ptu("mcp__local-mac__mail__read", {"limit": 1})
    assert [k["tool_name"] for k in seen["learn"]] == ["mac mail read", "mac mail read"]
    assert seen["learn"][0]["tool_result"] == {"messages": []}


def test_mcp_tools_mac_does_not_replace_keep_their_short_name(ptu):
    seen = ptu("mcp__taskfw__tasks__get", {"task_id": "x"})
    assert [k["tool_name"] for k in seen["learn"]] == ["tasks__get"]


# --- PreToolUse fail-closed ---------------------------------------------------

@pytest.mark.parametrize("tool_name, tool_input", [
    ("Bash", {"command": "~/bin/mac imessage send +1555 hi"}),
    ("mcp__local-mac__imessage__send", {"recipient": "x"}),
])
def test_a_failing_pretooluse_handler_denies_an_outbound_send(monkeypatch, capsys, tool_name, tool_input):
    import hooks.dispatcher as d
    monkeypatch.setattr(d, "read_stdin", lambda: {"tool_name": tool_name, "tool_input": tool_input,
                                                  "session_id": "s"})
    monkeypatch.setitem(d._HANDLERS, "PreToolUse", lambda _: 1 / 0)
    monkeypatch.setattr(sys, "argv", ["dispatcher.py", "PreToolUse"])
    d.main()
    out = capsys.readouterr().out
    assert '"permissionDecision": "deny"' in out and "mac imessage send" in out


# --- keywords and groups --------------------------------------------------------

def test_mac_hint_keywords_split_on_spaces_and_drop_mac():
    from langchain_learning.nodes.log_tool_usage import _derive_keywords
    from langchain_learning.nodes.update_tool_keywords import _tool_keywords
    for derive in (_derive_keywords, _tool_keywords):
        kw = derive("mac vault section_search", "vault", "").split(",")
        assert "mac" not in kw and {"vault", "section", "search"} <= set(kw)


def test_mac_hints_group_by_area_and_point_at_the_cli():
    from langchain_learning.tool_groups import format_groups, route_groups
    routes = route_groups([{"tool_name": "mac mail read"}, {"tool_name": "mac mail search"}],
                          graph={"nodes": [], "edges": []})
    assert [(r["group"], r["matched"]) for r in routes] == [("mac mail", ["mac mail read", "mac mail search"])]
    assert "mac mail --help" in format_groups(routes)[1]


# --- hint migration -------------------------------------------------------------

def _migration():
    spec = importlib.util.spec_from_file_location("migrate_hints_to_mac", ROOT / "scripts/migrate_hints_to_mac.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_maps_short_names_by_server_convention():
    m = _migration()
    ids = mc.known_ids()
    assert m.mac_id("vault__write", ids) == "mac vault write"
    assert m.mac_id("aq__planets", ids) == "mac astrology aq_planets"
    assert m.mac_id("db_init_state", ids) == "mac wyckoff db_init_state"
    assert m.mac_id("getComplexity", ids) == "mac sysml get_complexity"
    assert m.mac_id("tasks__get", ids) is None
    assert m.mac_id("navigate", ids) is None


def test_migration_folds_an_old_row_into_an_existing_mac_row():
    m = _migration()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE mcp_tool_hints (tool_name TEXT PRIMARY KEY, count INTEGER, last_used TEXT, "
                 "avg_latency_ms REAL, keywords TEXT, skill TEXT, recent_prompts TEXT, embedding BLOB)")
    conn.execute("INSERT INTO mcp_tool_hints VALUES ('mail__read', 3, '2026-01-01', 10, 'mail,read', 's', '[\"a\"]', NULL)")
    conn.execute("INSERT INTO mcp_tool_hints VALUES ('mac mail read', 1, '2026-02-01', 30, 'mac,inbox', '', '[\"b\"]', NULL)")
    m.migrate(conn, m.plan(conn, mc.known_ids()))
    rows = conn.execute("SELECT tool_name, count, last_used, avg_latency_ms, keywords, skill, recent_prompts "
                        "FROM mcp_tool_hints").fetchall()
    assert rows == [("mac mail read", 4, "2026-02-01", 15.0, "inbox,mail,read", "s", '["a", "b"]')]
