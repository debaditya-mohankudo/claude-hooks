"""MCPServer dispatcher for claude-hooks MCP server.

Maps domain → (module, [actions]). Each action becomes a tool named domain__action.
Add new tool modules here as they are migrated in.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path

from mcp.server import MCPServer

# Ensure src/ is on the path for relative imports within tool modules
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_module(module_path: str):
    """Load a module by dotted name (relative to src/) or absolute file path."""
    return importlib.import_module(module_path)


def _wrap(domain: str, handler):
    is_async = inspect.iscoroutinefunction(handler)
    if is_async:
        async def wrapped(**kwargs):
            return await handler(**kwargs)
    else:
        def wrapped(**kwargs):
            return handler(**kwargs)
    wrapped.__name__ = handler.__name__
    wrapped.__doc__ = handler.__doc__
    wrapped.__wrapped__ = handler
    return wrapped


DOMAIN_MAP: dict[str, tuple[str, list[str]]] = {
    "hooks":   ("tools.hooks",   ["checkpoint_query", "read_logs_sqlite", "server_memory", "session_id"]),
    "memory":  ("tools.memory",  ["add", "add_batch", "search", "list", "get",
                                  "tool_hints", "read_compact", "delete"]),
    # "tasks" removed here (task:87ec7876), along with tools/tasks.py — the whole
    # tasks__* surface (create, set_active, finish, ...) belonged to it. Task
    # storage and its MCP tools live in task-framework now.
    "code_rag": ("tools.code_rag", ["query", "smart_search", "index_files"]),
    "diff_rag": ("tools.diff_rag", ["query", "smart_search", "index_commits"]),
    "think":    ("tools.think",    ["think"]),
    "scratch":  ("tools.scratch",  ["set", "get", "list", "delete", "clear"]),
    # "concept" removed here (task:756c14db), along with tools/concept.py — a
    # duplicate of task-framework's own concept__* tools, same on-disk format,
    # same repo-explicit-no-default contract. Kept alive only by inertia until
    # a shape mismatch in taskfw's concept__get (fixed there first) made
    # removing this one safe. concept_store/store.py itself stays — the
    # extract-concepts skill and symbol_resolver.py still use ConceptStore
    # directly, unrelated to this MCP wrapper. extractor.py/claude_cli.py/
    # diff.py/diff_hook.py (LLM one-shot extraction + drift-detection hook)
    # were retired in task:85e6001f — the extract-concepts skill is the
    # extraction path now.
}


def build_dispatcher(mcp: MCPServer) -> None:
    for domain, (module_path, actions) in DOMAIN_MAP.items():
        module = _load_module(module_path)
        for action in actions:
            handler = getattr(module, f"handle_{action}")
            mcp.tool(name=f"{domain}__{action}")(_wrap(domain, handler))
