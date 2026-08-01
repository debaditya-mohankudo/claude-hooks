"""Code RAG MCP tool — semantic search over a repo's .code_embeddings.tvim index.

The index is built by `scripts/build_code_embeddings.py` in the target repo.
Embed model: nomic-embed-text via Ollama (768-dim) — same as vault_rag.

MCP tools exposed:
  code_rag__query(query, repo, k=5)             — pure vector semantic search
  code_rag__smart_search(query, repo, k=5)      — symbol FTS + vector hybrid rerank
  code_rag__index_files(files, repo)            — incremental upsert for changed files
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
from llama_index.embeddings.ollama import OllamaEmbedding

from tools.rag_core import load_index, query_index

_MODEL_NAME    = "nomic-embed-text"
_TVIM_NAME     = ".code_embeddings.tvim"
_META_NAME     = ".code_embeddings.meta.json"
_SNIPPET_LINES = 15

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


def _read_snippet(repo: Path, file: str, line: int) -> str:
    path = repo / file
    if not path.exists():
        return "(source not found)"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, line - 1)
    end   = min(len(lines), start + _SNIPPET_LINES)
    return "\n".join(lines[start:end])


def handle_query(query: str, repo: str = "", k: int = 5) -> str:
    """Semantic search over a repo's code embeddings index.

    Uses Ollama nomic-embed-text in-process (same model as vault_rag).

    Args:
        query: Natural language question or symbol name.
        repo:  Absolute path to the repo root. Defaults to claude-hooks.
        k:     Number of results to return (default 5).
    """
    repo_path = _resolve_repo(repo)
    tvim_path = repo_path / _TVIM_NAME
    meta_path = repo_path / _META_NAME

    index, meta = load_index(tvim_path, meta_path)
    if index is None:
        return (
            f"Code RAG index not found at {repo_path}. "
            "Run: uv run python scripts/build_code_embeddings.py"
        )

    commit      = meta.get("__commit__", "unknown")
    chunk_count = sum(1 for k in meta if k != "__commit__")

    q_vec = np.array([_get_embed_model().get_text_embedding(query)], dtype=np.float32)
    hits  = query_index(index, meta, q_vec, k=k)

    if not hits:
        return "No relevant chunks found."

    parts = [f"Code RAG — {chunk_count} chunks, commit {commit[:12]}\n"]
    for i, h in enumerate(hits, 1):
        file    = h.get("file", "?")
        line    = h.get("line", 1)
        name    = h.get("name", "?")
        kind    = h.get("kind", "?")
        score   = h["score"]
        snippet = _read_snippet(repo_path, file, line)
        parts.append(
            f"### #{i} [{score:.3f}] {kind} `{name}`\n"
            f"**{file}:{line}**\n\n"
            f"```python\n{snippet}\n```"
        )

    return "\n\n---\n\n".join(parts)


def _load_code_graph(repo: Path) -> dict:
    """Load .code_graph.json from repo root. Returns {} if not found."""
    path = repo / ".code_graph.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _symbol_fts(graph: dict, query: str, meta: dict) -> list[int]:
    """Return chunk IDs present in meta matching query via symbol_index.

    Strategy: exact match first, then substring match on symbol names.
    IDs are looked up from meta (the live index) by matching file+name —
    avoids hash recomputation and guarantees IDs exist in the index.
    """
    symbol_index: dict = graph.get("symbol_index", {})
    modules_info: dict = graph.get("modules", {})
    q_lower = query.lower()

    matched_modules: list[str] = []
    if query in symbol_index:
        matched_modules.extend(symbol_index[query])
    if not matched_modules:
        for sym, mods in symbol_index.items():
            if q_lower in sym.lower():
                matched_modules.extend(mods)

    targets: list[tuple[str, str]] = []
    for mod_key in matched_modules:
        info = modules_info.get(mod_key, {})
        file = info.get("file", "")
        if not file:
            continue
        for sym in info.get("symbols", []):
            sym_name = sym.get("name", "")
            if q_lower in sym_name.lower():
                targets.append((file, sym_name))

    ids = []
    for cid, info in meta.items():
        if cid == "__commit__":
            continue
        if (info.get("file", ""), info.get("name", "")) in targets:
            ids.append(int(cid))
    return ids


def handle_smart_search(query: str, repo: str = "", k: int = 5) -> str:
    """Hybrid search: symbol FTS over code graph + TurboVec semantic rerank.

    Pipeline:
      1. Symbol FTS — look up query in .code_graph.json symbol_index (exact then substring)
      2. If hits → TurboVec reranks only those candidates (allowlist search)
      3. If no FTS hits → pure vector search across full index
      4. Falls back to symbol-only if semantic index not built

    Args:
        query: Symbol name or natural-language concept.
        repo:  Absolute path to repo root. Defaults to claude-hooks.
        k:     Number of results (default 5).
    """
    repo_path = _resolve_repo(repo)
    tvim_path = repo_path / _TVIM_NAME
    meta_path = repo_path / _META_NAME

    graph        = _load_code_graph(repo_path)
    index, meta  = load_index(tvim_path, meta_path)
    fts_ids      = _symbol_fts(graph, query, meta) if graph else []

    if index is None:
        if not fts_ids:
            return f"No results found for: {query} (semantic index not built)"
        parts = []
        for cid in fts_ids[:k]:
            info = meta.get(str(cid), {})
            file = info.get("file", "?")
            line = info.get("line", 1)
            name = info.get("name", "?")
            parts.append(f"**{name}** — {file}:{line}")
        return "\n".join(parts)

    commit      = meta.get("__commit__", "unknown")
    chunk_count = sum(1 for ky in meta if ky != "__commit__")

    q_vec = np.array([_get_embed_model().get_text_embedding(query)], dtype=np.float32)

    if fts_ids:
        allowlist = np.array(fts_ids, dtype=np.uint64)
        scores, ids = index.search(q_vec, k=k, allowlist=allowlist)
        mode = "symbol+vector"
    else:
        scores, ids = index.search(q_vec, k=k)
        mode = "vector-only"

    hits = []
    for score, doc_id in zip(scores[0], ids[0]):
        info = meta.get(str(doc_id), {})
        hits.append({"id": int(doc_id), "score": float(score), **info})

    if not hits:
        return "No relevant chunks found."

    parts = [f"Code smart search ({mode}) — {chunk_count} chunks, commit {commit[:12]}\n"]
    for i, h in enumerate(hits, 1):
        file    = h.get("file", "?")
        line    = h.get("line", 1)
        name    = h.get("name", "?")
        kind    = h.get("kind", "?")
        score   = h["score"]
        snippet = _read_snippet(repo_path, file, line)
        parts.append(
            f"### #{i} [{score:.3f}] {kind} `{name}`  ({mode})\n"
            f"**{file}:{line}**\n\n"
            f"```python\n{snippet}\n```"
        )
    return "\n\n---\n\n".join(parts)


def handle_index_files(files: list[str], repo: str = "") -> str:
    """Incrementally update the code RAG index for specific files.

    Shells out to scripts/build_code_embeddings.py --files in the target repo's venv.

    Args:
        files: Repo-relative file paths (e.g. ["langchain_learning/nodes/load_turn.py"])
        repo:  Absolute path to repo root. Defaults to claude-hooks.
    """
    repo_path = _resolve_repo(repo)
    script    = repo_path / "scripts" / "build_code_embeddings.py"
    if not script.exists():
        return f"build_code_embeddings.py not found in {repo_path}"

    try:
        result = subprocess.run(
            ["uv", "run", "python", str(script), "--files", *files],
            cwd=repo_path, capture_output=True, text=True, timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return f"Indexing failed:\n{output}"
        last = next((l for l in reversed(output.splitlines()) if l.strip()), output)
        return last
    except subprocess.TimeoutExpired:
        return "Indexing timed out after 120s."
    except Exception as exc:
        return f"Indexing error: {exc}"
