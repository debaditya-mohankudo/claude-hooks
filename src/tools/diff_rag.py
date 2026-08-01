"""Diff RAG MCP tool — semantic search over git diff history (.diff_embeddings.tvim).

The index is built by `scripts/build_diff_embeddings.py`.
Embed model: nomic-embed-text via Ollama (768-dim) — same as code_rag and vault_rag.

MCP tools exposed:
  diff_rag__query(query, repo, k=5)          — semantic search over all indexed hunks
  diff_rag__smart_search(query, repo, k=5,   — semantic search with author/file filters
                         author, file_pattern)
  diff_rag__index_commits(repo, since,        — (re)build index via build_diff_embeddings.py
                          max_commits)
"""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

import numpy as np
from llama_index.embeddings.ollama import OllamaEmbedding

from tools.rag_core import load_index, query_index

_MODEL_NAME = "nomic-embed-text"
_TVIM_NAME  = ".diff_embeddings.tvim"
_META_NAME  = ".diff_embeddings.meta.json"
_INDEXED_KEY = "__indexed_commits__"

_DEFAULT_REPO = Path.home() / "workspace/claude-hooks"


def _resolve_repo(repo: str) -> Path:
    if not repo:
        return _DEFAULT_REPO
    p = Path(repo).expanduser()
    if not p.is_dir():
        raise ValueError(f"Repo not found: {repo}")
    return p


def _get_embed_model() -> OllamaEmbedding:
    return OllamaEmbedding(model_name=_MODEL_NAME)


def _format_hit(i: int, h: dict) -> str:
    commit  = h.get("commit_hash", "?")[:12]
    author  = h.get("author", "?")
    date    = h.get("date", "?")
    file    = h.get("file", "?")
    header  = h.get("hunk_header", "")
    snippet = h.get("snippet", "")
    score   = h["score"]
    return (
        f"### #{i} [{score:.3f}] `{file}`\n"
        f"**Commit:** {commit} | **Author:** {author} | **Date:** {date}\n"
        f"`{header}`\n\n"
        f"```diff\n{snippet}\n```"
    )


def handle_query(query: str, repo: str = "", k: int = 5) -> str:
    """Semantic search over indexed git diff hunks.

    Args:
        query: Natural language question, e.g. "changes related to auth logic"
        repo:  Absolute path to the git repo. Defaults to claude-hooks.
        k:     Number of results to return (default 5).
    """
    repo_path = _resolve_repo(repo)
    index, meta = load_index(repo_path / _TVIM_NAME, repo_path / _META_NAME)

    if index is None:
        return (
            f"Diff RAG index not found at {repo_path}. "
            "Run: uv run python scripts/build_diff_embeddings.py"
        )

    chunk_count = sum(1 for k in meta if k != _INDEXED_KEY)
    commit_count = len(meta.get(_INDEXED_KEY, []))

    q_vec = np.array([_get_embed_model().get_text_embedding(query)], dtype=np.float32)
    hits  = query_index(index, meta, q_vec, k=k)

    if not hits:
        return "No relevant hunks found."

    parts = [f"Diff RAG — {chunk_count} hunks across {commit_count} commits\n"]
    for i, h in enumerate(hits, 1):
        parts.append(_format_hit(i, h))

    return "\n\n---\n\n".join(parts)


def handle_smart_search(
    query: str,
    repo: str = "",
    k: int = 5,
    author: str = "",
    file_pattern: str = "",
) -> str:
    """Semantic search with optional author and file-pattern filters.

    Filters are applied to the metadata before semantic reranking — only
    matching chunks are searched, which sharpens results considerably.

    Args:
        query:        Natural language question or concept.
        repo:         Absolute path to the git repo. Defaults to claude-hooks.
        k:            Number of results (default 5).
        author:       Substring match on commit author name (case-insensitive).
        file_pattern: Glob pattern for file path, e.g. "src/tools/*.py".
    """
    repo_path = _resolve_repo(repo)
    index, meta = load_index(repo_path / _TVIM_NAME, repo_path / _META_NAME)

    if index is None:
        return (
            f"Diff RAG index not found at {repo_path}. "
            "Run: uv run python scripts/build_diff_embeddings.py"
        )

    # Build allowlist from metadata filters
    allowlist_ids: list[int] | None = None
    if author or file_pattern:
        allowlist_ids = []
        for cid, info in meta.items():
            if cid == _INDEXED_KEY:
                continue
            if author and author.lower() not in info.get("author", "").lower():
                continue
            if file_pattern and not fnmatch.fnmatch(info.get("file", ""), file_pattern):
                continue
            allowlist_ids.append(int(cid))

        if not allowlist_ids:
            return f"No indexed hunks match filters — author={author!r}, file_pattern={file_pattern!r}"

    q_vec = np.array([_get_embed_model().get_text_embedding(query)], dtype=np.float32)

    if allowlist_ids is not None:
        allowlist = np.array(allowlist_ids, dtype=np.uint64)
        scores, ids = index.search(q_vec, k=k, allowlist=allowlist)
        hits = [{"id": int(d), "score": float(s), **meta.get(str(d), {})}
                for s, d in zip(scores[0], ids[0])]
        mode = "filtered+vector"
    else:
        hits  = query_index(index, meta, q_vec, k=k)
        mode  = "vector-only"

    if not hits:
        return "No relevant hunks found."

    chunk_count  = sum(1 for k in meta if k != _INDEXED_KEY)
    commit_count = len(meta.get(_INDEXED_KEY, []))

    parts = [f"Diff smart search ({mode}) — {chunk_count} hunks, {commit_count} commits\n"]
    for i, h in enumerate(hits, 1):
        parts.append(_format_hit(i, h))

    return "\n\n---\n\n".join(parts)


def handle_index_commits(
    repo: str = "",
    since: str = "",
    max_commits: int = 0,
) -> str:
    """Build or incrementally update the diff RAG index.

    Shells out to scripts/build_diff_embeddings.py in the target repo's venv.

    Args:
        repo:        Absolute path to repo root. Defaults to claude-hooks.
        since:       Git ref to start from, e.g. "HEAD~50". Omit for incremental.
        max_commits: Cap the number of commits to process (0 = no limit).
    """
    repo_path = _resolve_repo(repo)
    script    = repo_path / "scripts" / "build_diff_embeddings.py"
    if not script.exists():
        return f"build_diff_embeddings.py not found in {repo_path}"

    cmd = ["uv", "run", "python", str(script)]
    if since:
        cmd += ["--since", since]
    if max_commits:
        cmd += ["--max-commits", str(max_commits)]

    try:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=300,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return f"Indexing failed:\n{output}"
        last = next((l for l in reversed(output.splitlines()) if l.strip()), output)
        return last
    except subprocess.TimeoutExpired:
        return "Indexing timed out after 300s."
    except Exception as exc:
        return f"Indexing error: {exc}"
