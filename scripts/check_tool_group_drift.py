"""Group-level drift check for ~/.claude/mcp-tools-domain.json.

Usage: python scripts/check_tool_group_drift.py LIVE_TOOLS.txt
LIVE_TOOLS.txt: one `mcp__server__group__tool` name per line (from the session's
ToolSearch/deferred tool list -- only visible in-session). Exit 1 if a live group has
no graph group or a graph group vanished from a live server; groups whose server isn't
loaded (claude-in-chrome, cmux-cua, built-ins) are listed as unverified, not failed.

Tags: tool-groups, drift, mcp-tools-domain
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langchain_learning.tool_groups import group_drift, load_graph  # noqa: E402


def main(path: str) -> int:
    graph = load_graph()
    if graph is None:
        print("graph unavailable")
        return 2
    res = group_drift(Path(path).read_text().split(), graph)
    for k in ("unregistered", "missing", "unverified"):
        print(f"{k}: {', '.join(res[k]) or '-'}")
    return 1 if res["unregistered"] or res["missing"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
