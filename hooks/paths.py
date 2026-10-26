"""Central path config for claude-hooks.

All project-relative paths resolve from this file's location so they remain
correct regardless of which git worktree the server runs from.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR    = PROJECT_ROOT / "hooks"
DOCS_DIR     = PROJECT_ROOT / "docs"
MEM_DB            = Path.home() / ".claude" / "MEMORY.sqlite"
# Was ~/workspace/claude_documents — that path doesn't exist, so vault-context
# reads (_load_vault_context in dispatcher.py) were silently returning nothing.
# The Obsidian vault actually lives under iCloud.
VAULT_ROOT        = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents"
TOOL_REGISTRY_PATH = (
    Path.home()
    / "Library/Mobile Documents/com~apple~CloudDocs/Databases/tool_registry.json"
)
# cli-graph's command graph: the list of `mac <area> <verb>` commands that
# hooks/core/mac_command.py validates calls against (task:0aa96180).
CLI_GRAPH_PATH = Path(os.environ.get("CLI_GRAPH_JSON") or
                      Path.home() / "workspace/cli-graph/graph/cli_graph.json")
