"""Central path config for claude-hooks.

All project-relative paths resolve from this file's location so they remain
correct regardless of which git worktree the server runs from.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR    = PROJECT_ROOT / "hooks"
DOCS_DIR     = PROJECT_ROOT / "docs"
MEM_DB            = Path.home() / ".claude" / "MEMORY.sqlite"
VAULT_ROOT        = Path.home() / "workspace" / "claude_documents"
TOOL_REGISTRY_PATH = (
    Path.home()
    / "Library/Mobile Documents/com~apple~CloudDocs/Databases/tool_registry.json"
)
