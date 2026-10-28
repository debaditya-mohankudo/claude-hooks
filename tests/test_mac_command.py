"""Which `mac <area> <verb>` commands a tool call ran (task:0aa96180)."""
import json

import pytest

from core import mac_command as mc

IDS = ["mac mail read", "mac mail delete", "mac vault write", "mac vault move", "mac vault append",
       "mac imessage send", "mac astrology aq_planets", "mac astrology d9", "mac wyckoff db_init_state",
       "mac sysml get_complexity"]


@pytest.fixture(autouse=True)
def graph(tmp_path, monkeypatch):
    path = tmp_path / "cli_graph.json"
    path.write_text(json.dumps({"nodes": [{"kind": "command", "id": i} for i in IDS], "edges": []}))
    monkeypatch.setattr(mc, "CLI_GRAPH_PATH", path)
    mc.known_ids.cache_clear()
    yield path
    mc.known_ids.cache_clear()


def ids(command):
    return [c.id for c in mc.parse_bash(command)]


@pytest.mark.parametrize("command", [
    "mac mail read --limit 5",
    "cd /x && mac mail read | jq .",
    "MAC_PYTHON=/x mac mail read",
    "~/bin/mac mail read",
    "/Users/someone/bin/mac mail read",
    "./bin/mac mail read",
    "env FOO=1 mac mail read",
])
def test_a_launcher_in_command_position_is_a_call(command):
    assert ids(command) == ["mac mail read"]


@pytest.mark.parametrize("command", [
    'grep -n "mac vault write" hooks/x.py',
    'git commit -m "mac imessage send now prompts"',
    "echo mac mail delete",
    "mac --help",
    "mac mail --help",
    "mac nosuch verb",
    "echo 'unbalanced",
    "cat <<'EOF' > f\nmac vault write x.md y\nEOF",
])
def test_text_naming_a_verb_is_not_a_call(command):
    assert ids(command) == []


def test_every_call_in_a_compound_command_is_found_with_its_args():
    calls = mc.parse_bash('mac vault write "A b.md" hi 2>&1; mac vault move a.md b.md > /dev/null')
    assert [(c.id, c.args) for c in calls] == [("mac vault write", ("A b.md", "hi")),
                                               ("mac vault move", ("a.md", "b.md"))]


def test_a_command_after_a_heredoc_is_still_found():
    assert ids("cat <<EOF > f\nmac vault write x.md y\nEOF\nmac vault append y.md z") == ["mac vault append"]


@pytest.mark.parametrize("tool, expected", [
    ("mcp__local-mac__vault__write", "mac vault write"),
    ("mcp__astrology__aq__planets", "mac astrology aq_planets"),
    ("mcp__astrology__astrology__d9", "mac astrology d9"),
    ("mcp__wyckoff-analyzer__db_init_state", "mac wyckoff db_init_state"),
    ("mcp__sysml-mcp__getComplexity", "mac sysml get_complexity"),
    ("mcp__local-mac__ollama_agent__chat", None),   # not a mac command
    ("mcp__taskfw__tasks__get", None),               # stays MCP
    ("Read", None),
])
def test_mcp_calls_map_to_their_mac_command(tool, expected):
    call = mc.from_mcp(tool, {"path": "x.md"})
    assert (call.id if call else None) == expected
    if call:
        assert call.params == {"path": "x.md"}


def test_without_the_graph_both_forms_map_on_syntax(graph):
    graph.unlink()
    mc.known_ids.cache_clear()
    assert ids("mac anything goes") == ["mac anything goes"]
    assert mc.from_mcp("mcp__local-mac__imessage__send").id == "mac imessage send"
    assert mc.from_mcp("mcp__taskfw__tasks__get") is None


def test_positionals_skip_flags_and_flag_values():
    assert mc.positionals(("--limit", "5", "--json", "a.md", "b.md")) == ["a.md", "b.md"]


def test_a_regenerated_graph_is_read_without_a_restart(graph):
    assert ids("mac memory list") == []
    graph.write_text(json.dumps({"nodes": [{"kind": "command", "id": "mac memory list"}], "edges": []}))
    assert ids("mac memory list") == ["mac memory list"]
