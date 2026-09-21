"""Mine consecutive MCP tool-call pairs from the claude-hooks PostToolUse log
(~/.claude/claude_hooks.sqlite, hook_logs rows `PTU enter: session=<id> tool=<name>`)
and merge them into ontology/mcp-tools-domain.json as `often_follows` edges.

Covers built-in tools (grouped under server claude-code) as well as MCP tools, and
seeds the four hook-event nodes (UserPromptSubmit/PreToolUse/PostToolUse/Stop).
Idempotent: strips prior often_follows/member_of edges and tool nodes first.
Usage: python3 scripts/mine_tool_sequences.py [--min-tool 2] [--min-group 3]
"""
import argparse, collections, json, os, re, sqlite3

GRAPH = os.path.join(os.path.dirname(__file__), "..", "ontology", "mcp-tools-domain.json")
HOOKS_DB = os.path.expanduser("~/.claude/claude_hooks.sqlite")
PTU = re.compile(r"PTU enter: session=(\S+) tool=(\S+) ")


# Claude Code built-in tools (not MCP) -> broad group; anything unlisted falls in "meta".
BUILTIN = {
    "core_editing": ("Bash", "Read", "Edit", "Write", "NotebookEdit"),
    "web_research": ("WebSearch", "WebFetch"),
    "orchestration": ("Agent", "SendMessage", "ListAgents", "TaskStop", "SubagentHandback",
                      "ScheduleWakeup", "Monitor"),
    "meta": ("ToolSearch", "Skill", "AskUserQuestion", "Artifact"),
}
BUILTIN_DEF = {
    "core_editing": "shell and file tools built into Claude Code",
    "web_research": "web search and fetch",
    "orchestration": "subagents, messaging, scheduling, background tasks",
    "meta": "tool discovery, skills, user questions, artifacts",
}
HOOKS = {  # event -> (definition, target category or scope, relation)
    "UserPromptSubmit": ("injects ranked memories, suggested tools and task history into the prompt before the model sees it", "memory_knowledge", "feeds"),
    "PreToolUse": ("gate_check: allow or deny a tool call (e.g. commit needs a task:<id> ref)", "scope:all_tool_calls", "intercepts"),
    "PostToolUse": ("log_tool_usage: records each call (latency, keywords) - the source of the often_follows edges", "scope:all_tool_calls", "intercepts"),
    "Stop": ("end-of-turn handler: session summary, drift nudges", "hooks_observability", "feeds"),
}


def seed(g):
    """Add built-in tool groups and hook-event nodes (idempotent, by id)."""
    have = {n["id"] for n in g["nodes"]}
    add = lambda n: n["id"] in have or (g["nodes"].append(n), have.add(n["id"]))
    add({"id": "server:claude-code", "label": "claude-code", "kind": "server",
         "definition": "Claude Code built-in tools"})
    add({"id": "scope:all_tool_calls", "label": "All tool calls", "kind": "scope",
         "definition": "every tool invocation, built-in and MCP"})
    for grp, tools in BUILTIN.items():
        gid_ = f"group:claude-code:{grp}"
        add({"id": gid_, "label": f"claude-code/{grp}", "kind": "tool_group",
             "category": "builtin_" + grp, "tools": ", ".join(tools)})
        add({"id": "builtin_" + grp, "label": "Builtin " + grp.replace("_", " ").title(),
             "kind": "category", "definition": BUILTIN_DEF[grp]})
        edges = [{"from": "server:claude-code", "to": gid_, "relation": "provides"},
                 {"from": gid_, "to": "builtin_" + grp, "relation": "in_category"}]
        g["edges"] += [e for e in edges if e not in g["edges"]]
    for ev, (d, tgt, rel) in HOOKS.items():
        add({"id": "hook:" + ev, "label": ev, "kind": "hook_event", "definition": d})
        e = {"from": "hook:" + ev, "to": tgt, "relation": rel}
        if e not in g["edges"]:
            g["edges"].append(e)
    g["relation_types"]["intercepts"] = "hook event runs on every call in the target scope, before (PreToolUse) or after (PostToolUse) it"


def builtin_group(name):
    return next((g for g, ts in BUILTIN.items() if name in ts), "meta")


def split(full):
    """mcp__server__group__tool -> (server, group, tool); group defaults to server.
    Built-in tools map to server claude-code and a broad BUILTIN group."""
    if not full.startswith("mcp__"):
        return "claude-code", builtin_group(full), full
    server, rest = full[len("mcp__"):].split("__", 1)
    group, _, tool = rest.rpartition("__")
    return server, group or server, tool


def sessions():
    con = sqlite3.connect(HOOKS_DB)
    out = collections.defaultdict(list)
    for (msg,) in con.execute("select message from hook_logs where message like 'PTU enter:%' order by id"):
        m = PTU.match(msg)
        if m:
            out[m[1]].append(m[2])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-tool", type=int, default=2)
    ap.add_argument("--min-group", type=int, default=3)
    a = ap.parse_args()

    g = json.load(open(GRAPH))
    g["nodes"] = [n for n in g["nodes"] if n["kind"] != "tool"]
    g["edges"] = [e for e in g["edges"] if e["relation"] not in ("often_follows", "member_of")]
    seed(g)
    group_ids = {n["id"] for n in g["nodes"] if n["kind"] == "tool_group"}

    def tid(full):
        return "tool:" + (full[5:] if full.startswith("mcp__") else "claude-code__" + full)

    def gid(full):
        s, grp, _ = split(full)
        return f"group:{s}:{grp}"

    tp, gp = collections.Counter(), collections.Counter()
    tout, gout = collections.Counter(), collections.Counter()
    seqs = {s: [t for t in seq if gid(t) in group_ids] for s, seq in sessions().items()}
    for seq in seqs.values():
        for x, y in zip(seq, seq[1:]):
            if x != y:
                tp[x, y] += 1; tout[x] += 1
            if gid(x) != gid(y):
                gp[gid(x), gid(y)] += 1; gout[gid(x)] += 1

    for (x, y), c in gp.most_common():
        if c >= a.min_group:
            g["edges"].append({"from": x, "to": y, "relation": "often_follows", "count": c, "p": round(c / gout[x], 2)})
    tnodes, tedges = set(), []
    for (x, y), c in tp.most_common():
        if c >= a.min_tool:
            tnodes |= {x, y}
            tedges.append({"from": tid(x), "to": tid(y), "relation": "often_follows",
                           "count": c, "p": round(c / tout[x], 2)})
    for t in sorted(tnodes):
        g["nodes"].append({"id": tid(t), "label": split(t)[2], "kind": "tool", "group": gid(t)})
        g["edges"].append({"from": tid(t), "to": gid(t), "relation": "member_of"})
    g["edges"] += tedges
    g["relation_types"]["often_follows"] = "mined from PostToolUse `PTU enter` log lines: target is called next after source within one session (count; p = share of the source's successors)"
    g["relation_types"]["member_of"] = "tool belongs to the tool group"
    g["meta"]["often_follows_source"] = "~/.claude/claude_hooks.sqlite hook_logs `PTU enter` lines; regenerate with scripts/mine_tool_sequences.py"
    json.dump(g, open(GRAPH, "w"), indent=2)
    print("sessions", len(seqs), "calls", sum(map(len, seqs.values())),
          "| group edges", sum(1 for c in gp.values() if c >= a.min_group),
          "| tool edges", len(tedges), "| tool nodes", len(tnodes))
    for e in sorted(tedges, key=lambda e: -e["count"])[:10]:
        print(e["count"], e["p"], e["from"][5:], "->", e["to"][5:])


main()
