"""Shared repo-path resolution for tools operating against another repo's
on-disk state (concept_store, repo_memory, ...). Extracted out of concept.py
so the same validation isn't copied per tool module and silently diverges."""
from __future__ import annotations

from pathlib import Path


def resolve_repo(repo: str) -> Path:
    """repo -> validated Path. No default-to-claude-hooks fallback: there is
    no sensible default target repo for tools whose whole purpose is
    operating on repos other than this one."""
    if not repo:
        raise ValueError("repo is required")
    p = Path(repo).expanduser()
    if not p.is_dir():
        raise ValueError(f"Repo not found: {repo}")
    return p
