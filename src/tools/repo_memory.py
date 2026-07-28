"""repo_memory__* MCP tools — thin wrappers around repo_memory/store.py's
RepoMemoryStore, for reading/writing repo-specific project/reference
memories inline during a session against a target repo's
repo_memory/memories.json.

`repo` is REQUIRED on every tool, same convention as concept.py's tools —
there is no sensible default target repo for a tool whose whole purpose is
operating on repos other than this one.
"""
from __future__ import annotations

from repo_memory.store import RepoMemoryStore
from tools._repo_resolve import resolve_repo as _resolve_repo


def _store_for(repo: str) -> RepoMemoryStore:
    return RepoMemoryStore(_resolve_repo(repo) / "repo_memory" / "memories.json")


def handle_get(repo: str, name: str) -> dict:
    """Get one repo memory by name. {"found": False} if it doesn't exist."""
    try:
        store = _store_for(repo)
    except ValueError as exc:
        return {"error": str(exc)}
    memory = store.get(name)
    if memory is None:
        return {"found": False}
    return {"found": True, "memory": memory}


def handle_list(repo: str, type: str = "") -> dict:
    """All repo memories, optionally filtered to type ("project"|"reference")."""
    try:
        store = _store_for(repo)
    except ValueError as exc:
        return {"error": str(exc)}
    memories = store.list(type=type or None)
    return {"memories": memories}


def handle_upsert(repo: str, memory: dict) -> dict:
    """Insert or update a repo memory. Requires memory["name"] and
    memory["type"] in ("project", "reference"). Creates repo_memory/
    (and memories.json) if this repo has neither yet — RepoMemoryStore
    tolerates a missing FILE, but save() has no mkdir, so a missing
    DIRECTORY would otherwise crash on first write."""
    if not memory.get("name"):
        return {"error": "memory['name'] is required"}
    try:
        repo_path = _resolve_repo(repo)
    except ValueError as exc:
        return {"error": str(exc)}
    store_dir = repo_path / "repo_memory"
    store_dir.mkdir(parents=True, exist_ok=True)
    store = RepoMemoryStore(store_dir / "memories.json")
    try:
        store.upsert(memory)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"ok": True, "name": memory["name"]}


def handle_delete(repo: str, name: str) -> dict:
    """Delete a repo memory by name. Not an error if it didn't exist."""
    try:
        store = _store_for(repo)
    except ValueError as exc:
        return {"error": str(exc)}
    existed = store.get(name) is not None
    store.delete(name)
    return {"ok": True, "deleted": existed}


def handle_search(repo: str, query: str) -> dict:
    """Free-text keyword search across name/body/tags.

    Unlike get() (needs the exact name) or list() (needs the exact type),
    this finds memories by what they say — case-insensitive substring
    match, ranked by how many fields matched.
    """
    if not query or not query.strip():
        return {"error": "query is required"}
    try:
        store = _store_for(repo)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"memories": store.search(query)}
