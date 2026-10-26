"""Which `mac <area> <verb>` commands a tool call ran (task:0aa96180).

Hooks key on the command id `mac <area> <verb>` rather than on MCP tool names,
so they behave the same whether a command ran as a Bash `mac` call or through
one of the MCP servers `mac` replaces (local-mac, astrology, wyckoff-analyzer,
sysml-mcp). calls_for() maps either form to MacCall values.

A Bash command line counts as a `mac` call only where a launcher (`mac`,
`~/bin/mac`, `.../bin/mac`) is the command word of a simple command, after any
VAR=value assignments. `grep "mac vault write" f` and a commit message naming a
verb are not calls. Heredoc bodies are skipped. A line shlex cannot tokenise
(an unbalanced quote) yields no calls.

Calls are checked against cli-graph's graph/cli_graph.json, so an MCP tool
with no `mac` verb (local-mac's ollama_agent) keeps its MCP name. When the graph
cannot be read, both forms are mapped on syntax alone: a safety hook keyed on
`mac imessage send` must not stop matching because a file is missing.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from functools import lru_cache

from hooks.paths import CLI_GRAPH_PATH

# MCP server -> mac area. local-mac's tools are `<area>__<verb>`, so it maps per tool.
SERVER_AREA = {"local-mac": None, "astrology": "astrology", "wyckoff-analyzer": "wyckoff", "sysml-mcp": "sysml"}

_SEPARATORS = {"&&", "||", ";", "|", "|&", "&", "\n", "(", ")", ";;"}
_REDIRECTS = {">", ">>", "<", "<<<", ">&", "<&", "&>", "&>>", ">|", "<>"}
_ASSIGNMENT =re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"command", "exec", "nohup", "time", "env"}
_AREA = re.compile(r"[a-z_]+")
_VERB = re.compile(r"[a-z_0-9]+")
_MCP = re.compile(r"mcp__([^_]+)__(.+)")


@dataclass(frozen=True)
class MacCall:
    area: str
    verb: str
    args: tuple[str, ...] = ()                 # argv after the verb (Bash form)
    params: dict = field(default_factory=dict, compare=False, hash=False)  # tool_input (MCP form)

    @property
    def id(self) -> str:
        return f"mac {self.area} {self.verb}"


@lru_cache(maxsize=1)
def known_ids() -> frozenset[str] | None:
    """Command ids in the graph, or None when it cannot be read."""
    try:
        graph = json.loads(CLI_GRAPH_PATH.read_text())
        return frozenset(n["id"] for n in graph["nodes"] if n.get("kind") == "command")
    except Exception:
        return None


def is_launcher(word: str) -> bool:
    return word == "mac" or word.endswith("/bin/mac")


def _tokens(command: str) -> list[str]:
    lex = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _simple_commands(tokens: list[str]) -> list[list[str]]:
    """Split a token stream at separators, dropping heredoc bodies."""
    out: list[list[str]] = [[]]
    heredocs: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("<<", "<<-") and i + 1 < len(tokens):
            heredocs.append(tokens[i + 1])
            i += 2
            continue
        if tok == "\n" and heredocs:
            delim = heredocs.pop(0)
            # Skip to the line holding only the delimiter.
            j = i + 1
            while j < len(tokens) and not (tokens[j] == delim and (j + 1 == len(tokens) or tokens[j + 1] == "\n")
                                            and tokens[j - 1] == "\n"):
                j += 1
            i = j + 1
            continue
        if tok in _REDIRECTS:
            # `> out`, `2>&1`: drop the operator, its target and an fd number before >&/<&.
            if tok in (">&", "<&") and out[-1] and out[-1][-1] in ("0", "1", "2"):
                out[-1].pop()
            i += 2
            continue
        if tok in _SEPARATORS:
            out.append([])
        else:
            out[-1].append(tok)
        i += 1
    return [c for c in out if c]


def parse_bash(command: str) -> list[MacCall]:
    """The `mac` calls a Bash command line makes, in order."""
    if "mac" not in command:
        return []
    try:
        tokens = _tokens(command)
    except ValueError:
        return []
    ids = known_ids()
    calls = []
    for words in _simple_commands(tokens):
        while words and (_ASSIGNMENT.match(words[0]) or words[0] in _WRAPPERS):
            words = words[1:]
        if len(words) < 3 or not is_launcher(words[0]):
            continue
        area, verb = words[1], words[2]
        if not (_AREA.fullmatch(area) and _VERB.fullmatch(verb)):
            continue
        call = MacCall(area, verb, tuple(words[3:]))
        if ids is None or call.id in ids:
            calls.append(call)
    return calls


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def from_mcp(tool_name: str, tool_input: dict | None = None) -> MacCall | None:
    """The `mac` command an MCP tool call corresponds to, or None.

    Verb naming follows cli-graph tools/gen_mcp_groups.verb_name.
    """
    m = _MCP.fullmatch(tool_name or "")
    if not m or m.group(1) not in SERVER_AREA:
        return None
    server, tool = m.groups()
    area = SERVER_AREA[server]
    if area is None:
        if "__" not in tool:
            return None
        area, verb = tool.split("__", 1)
    else:
        verb = _snake(tool.removeprefix(f"{area}__").replace("__", "_").replace("-", "_"))
    ids = known_ids()
    call = MacCall(area, verb, (), dict(tool_input or {}))
    return call if ids is None or call.id in ids else None


def calls_for(tool_name: str, tool_input: dict | None) -> list[MacCall]:
    """The `mac` commands a hook payload's tool call ran (empty for anything else)."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        return parse_bash(tool_input.get("command") or "")
    call = from_mcp(tool_name, tool_input)
    return [call] if call else []


def positionals(args: tuple[str, ...], value_flags: tuple[str, ...] = ("--limit",)) -> list[str]:
    """Positional arguments in a `mac` argv, skipping flags and the values of value_flags."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in value_flags:
            skip = True
        elif a.startswith("-") and a != "-":
            continue
        else:
            out.append(a)
    return out

