"""Mine consecutive MCP tool-call pairs from the claude-hooks PostToolUse log
(~/.claude/claude_hooks.sqlite, hook_logs rows `PTU enter: session=<id> tool=mcp__...`)
and merge them into ontology/mcp-tools-domain.json as `often_follows` edges.

Non-MCP tools (Bash, Read, ...) are ignored, so a pair may have one between it.
Idempotent: strips prior often_follows/member_of edges and tool nodes first.
Usage: python3 scripts/mine_tool_sequences.py [--min-tool 2] [--min-group 3]
"""
import argparse, collections, json, os, re, sqlite3

GRAPH = os.path.join(os.path.dirname(__file__), "..", "ontology", "mcp-tools-domain.json")
HOOKS_DB = os.path.expanduser("~/.claude/claude_hooks.sqlite")
PTU = re.compile(r"PTU enter: session=(\S+) tool=(mcp__\S+) ")


def split(full):
    """mcp__server__group__tool -> (server, group, tool); group defaults to server."""
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
    group_ids = {n["id"] for n in g["nodes"] if n["kind"] == "tool_group"}

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
            tedges.append({"from": "tool:" + x[5:], "to": "tool:" + y[5:], "relation": "often_follows",
                           "count": c, "p": round(c / tout[x], 2)})
    for t in sorted(tnodes):
        g["nodes"].append({"id": "tool:" + t[5:], "label": split(t)[2], "kind": "tool", "group": gid(t)})
        g["edges"].append({"from": "tool:" + t[5:], "to": gid(t), "relation": "member_of"})
    g["edges"] += tedges
    g["relation_types"]["often_follows"] = "mined from PostToolUse `PTU enter` log lines: target is called next (MCP calls only) after source within one session (count; p = share of the source's successors)"
    g["relation_types"]["member_of"] = "tool belongs to the tool group"
    g["meta"]["often_follows_source"] = "~/.claude/claude_hooks.sqlite hook_logs `PTU enter` lines; regenerate with scripts/mine_tool_sequences.py"
    json.dump(g, open(GRAPH, "w"), indent=2)
    print("sessions", len(seqs), "mcp calls", sum(map(len, seqs.values())),
          "| group edges", sum(1 for c in gp.values() if c >= a.min_group),
          "| tool edges", len(tedges), "| tool nodes", len(tnodes))
    for e in sorted(tedges, key=lambda e: -e["count"])[:10]:
        print(e["count"], e["p"], e["from"][5:], "->", e["to"][5:])


main()
