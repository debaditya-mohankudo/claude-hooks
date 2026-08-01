#!/usr/bin/env python3
"""Query .code_embeddings.tvim for top-k chunks matching a natural language query.

Usage:
    uv run python scripts/query_code_embeddings.py "how does compaction work" --k 5
    uv run python scripts/query_code_embeddings.py "domain scoring" --k 3
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

REPO_ROOT    = Path(__file__).resolve().parent.parent
TVIM_FILE    = REPO_ROOT / ".code_embeddings.tvim"
META_FILE    = REPO_ROOT / ".code_embeddings.meta.json"
MODEL_NAME   = "nomic-embed-text"  # Ollama local model, 768-dim
SNIPPET_LINES = 15


def _load_index():
    import turbovec
    if not TVIM_FILE.exists() or not META_FILE.exists():
        raise FileNotFoundError(
            f"{TVIM_FILE.name} not found — run: uv run python scripts/build_code_embeddings.py"
        )
    index  = turbovec.IdMapIndex.load(str(TVIM_FILE))
    meta   = json.loads(META_FILE.read_text())
    commit = meta.get("__commit__", "unknown")
    return index, meta, commit


def _embed_query(text: str) -> np.ndarray:
    from llama_index.embeddings.ollama import OllamaEmbedding
    model = OllamaEmbedding(model_name=MODEL_NAME)
    return np.array([model.get_text_embedding(text)], dtype=np.float32)


def _read_snippet(file: str, line: int) -> str:
    path = REPO_ROOT / file
    if not path.exists():
        return "(source not found)"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, line - 1)
    end   = min(len(lines), start + SNIPPET_LINES)
    return "\n".join(lines[start:end])


def query(text: str, k: int = 5) -> list[dict]:
    index, meta, commit = _load_index()
    chunk_count = len([k for k in meta if k != "__commit__"])
    print(f"Index: {chunk_count} chunks, commit {commit[:12]}")

    q_vec = _embed_query(text)
    scores, ids = index.search(q_vec, k=k)

    results = []
    for score, doc_id in zip(scores[0], ids[0]):
        info = meta.get(str(doc_id), {})
        results.append({
            "score":   float(score),
            "module":  info.get("module", "?"),
            "file":    info.get("file", "?"),
            "name":    info.get("name", "?"),
            "kind":    info.get("kind", "?"),
            "line":    info.get("line", 0),
            "snippet": _read_snippet(info.get("file", ""), info.get("line", 1)),
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Natural language query")
    parser.add_argument("--k", type=int, default=5, help="Number of results")
    args = parser.parse_args()

    results = query(args.query, k=args.k)
    for i, r in enumerate(results, 1):
        print(f"\n{'='*60}")
        print(f"#{i} [{r['score']:.3f}] {r['kind']} {r['name']}")
        print(f"    {r['file']}:{r['line']}")
        print(f"{'─'*60}")
        print(r["snippet"])


if __name__ == "__main__":
    main()
