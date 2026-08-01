#!/usr/bin/env python3
"""Compare docs-only vs full-repo RAG indexes against 10 real task titles.

Usage:
    uv run python scripts/compare_rag_indexes.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

REPO_ROOT  = Path(__file__).resolve().parent.parent
MODEL_NAME = "nomic-embed-text"

DOCS_TVIM  = REPO_ROOT / ".code_embeddings.tvim"
DOCS_META  = REPO_ROOT / ".code_embeddings.meta.json"
FULL_TVIM  = REPO_ROOT / ".fullrepo_embeddings.tvim"
FULL_META  = REPO_ROOT / ".fullrepo_embeddings.meta.json"

TASK_TITLES = [
    "Fix load_memories always-include pool: move p>1 domain rows to BM25 scored pool",
    "Domain-scoped always-inject — p=1 fires only when domain is active",
    "Add recency boost to BM25 scoring in memory_loader_lc.py",
    "Expand server memory to record all tool calls (Bash, Read, Edit, Write + MCP)",
    "Log injected memory IDs to task_events per prompt turn",
    "Add missing log statements to gates.py",
    "Replace BM25 loop in LoadMemoriesNode with dense vector search",
    "Build scripts/build_memories_embeddings.py — embed MEMORY.sqlite rows into TurboVec",
    "Task Review Gate — intermediate review state between open and done",
    "Drop priority col from MEMORY.sqlite + update MCP memory tools",
]

TOP_K = 3


def _embed(texts: list[str]) -> np.ndarray:
    from llama_index.embeddings.ollama import OllamaEmbedding
    model = OllamaEmbedding(model_name=MODEL_NAME)
    vecs = model.get_text_embedding_batch(texts, show_progress=False)
    return np.array(vecs, dtype=np.float32)


def _load(tvim: Path, meta_path: Path):
    import turbovec
    index = turbovec.IdMapIndex.load(str(tvim))
    meta  = json.loads(meta_path.read_text())
    return index, meta


def _query(index, meta: dict, vec: np.ndarray, k: int = TOP_K) -> list[dict]:
    scores, ids = index.search(vec.reshape(1, -1), k=k)
    results = []
    for score, uid in zip(scores[0], ids[0]):
        info = meta.get(str(uid), {})
        results.append({
            "score": float(score),
            "file":  info.get("file", "?"),
            "name":  info.get("name", "?"),
            "kind":  info.get("kind", "?"),
        })
    return results


def main() -> None:
    print("Loading indexes...")
    if not DOCS_TVIM.exists():
        print(f"  [MISSING] {DOCS_TVIM.name} — run build_code_embeddings.py first")
        return
    if not FULL_TVIM.exists():
        print(f"  [MISSING] {FULL_TVIM.name} — run build_fullrepo_embeddings.py first")
        return

    docs_index, docs_meta = _load(DOCS_TVIM, DOCS_META)
    full_index, full_meta = _load(FULL_TVIM, FULL_META)

    docs_chunks = sum(1 for k in docs_meta if k != "__commit__")
    full_chunks = sum(1 for k in full_meta if k != "__commit__")
    print(f"  docs-only : {docs_chunks} chunks")
    print(f"  full-repo : {full_chunks} chunks")
    print()

    print("Embedding queries...")
    vecs = _embed(TASK_TITLES)

    for i, (title, vec) in enumerate(zip(TASK_TITLES, vecs), 1):
        print(f"{'─' * 80}")
        print(f"[{i:02d}] {title}")
        print()

        docs_hits = _query(docs_index, docs_meta, vec)
        full_hits = _query(full_index, full_meta, vec)

        print(f"  {'DOCS-ONLY':<38}  {'FULL-REPO'}")
        for j in range(TOP_K):
            d = docs_hits[j] if j < len(docs_hits) else {}
            f = full_hits[j] if j < len(full_hits) else {}
            d_str = f"[{d['score']:.3f}] {d['file']}:{d['name']}" if d else ""
            f_str = f"[{f['score']:.3f}] {f['file']}:{f['name']}" if f else ""
            print(f"  {d_str:<38}  {f_str}")
        print()


if __name__ == "__main__":
    main()
