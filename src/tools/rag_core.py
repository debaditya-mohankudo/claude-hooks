"""Generic TurboVec RAG core — shared by vault_rag and code_rag.

Both indexes use TurboVec IdMapIndex (.tvim) + a .meta.json sidecar.
The only difference between them is the embed model and what the metadata
dict contains — so the core is embed-model-agnostic: callers pass in a
precomputed query vector.

Public API
----------
load_index(tvim_path, meta_path)  → (IdMapIndex, meta_dict) | (None, {})
save_index(index, meta, tvim_path, meta_path)  — atomic write
query_index(index, meta, q_vec, k)  → list[dict]  # {id, score, **meta_fields}
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import turbovec


def load_index(tvim_path: Path, meta_path: Path) -> tuple:
    """Load a TurboVec index + metadata sidecar. Returns (None, {}) if not found."""
    if not tvim_path.exists() or not meta_path.exists():
        return None, {}
    try:
        index = turbovec.IdMapIndex.load(str(tvim_path))
        meta  = json.loads(meta_path.read_text())
        return index, meta
    except Exception:
        return None, {}


def save_index(index, meta: dict, tvim_path: Path, meta_path: Path) -> None:
    """Atomic write: write to .tmp files then rename into place."""
    tmp_tvim = tvim_path.with_suffix(".tvim.tmp")
    tmp_meta = meta_path.with_suffix(".json.tmp")
    index.write(str(tmp_tvim))
    tmp_meta.write_text(json.dumps(meta))
    tmp_tvim.rename(tvim_path)
    tmp_meta.rename(meta_path)


def query_index(index, meta: dict, q_vec: np.ndarray, k: int) -> list[dict]:
    """Search index with a precomputed query vector. Returns top-k results.

    Args:
        index:  turbovec.IdMapIndex (already loaded + prepared)
        meta:   id → metadata dict mapping (string keys)
        q_vec:  shape (1, D) float32 query embedding
        k:      number of results

    Returns list of dicts: {id, score, **whatever fields are in meta[id]}
    """
    scores, ids = index.search(q_vec, k=k)
    results = []
    for score, doc_id in zip(scores[0], ids[0]):
        info = meta.get(str(doc_id), {})
        results.append({"id": int(doc_id), "score": float(score), **info})
    return results
