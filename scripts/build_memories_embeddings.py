#!/usr/bin/env python3
"""Build TurboVec RAG embeddings for MEMORY.sqlite memories.

Each memory row is embedded as "name: <name>\ntags: <tags>\n<body>" via
Ollama (nomic-embed-text). Stable uint64 IDs are derived from sha256(name)
so incremental upserts work without a full rebuild.

Output (iCloud Databases directory):
  memories_embeddings.tvim      — TurboVec IdMapIndex
  memories_embeddings.meta.json — row metadata sidecar

Usage:
    uv run python scripts/build_memories_embeddings.py          # full rebuild
    uv run python scripts/build_memories_embeddings.py --name slug1 slug2
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

REPO_ROOT  = Path(__file__).resolve().parent.parent
ICLOUD_DB  = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Databases"
TVIM_FILE  = ICLOUD_DB / "memories_embeddings.tvim"
META_FILE  = ICLOUD_DB / "memories_embeddings.meta.json"
MEMORY_DB  = Path.home() / ".claude" / "MEMORY.sqlite"
MODEL_NAME = "nomic-embed-text"


# ---------------------------------------------------------------------------
# Stable chunk IDs
# ---------------------------------------------------------------------------

def _memory_id(name: str) -> int:
    """Deterministic uint64 from memory name — stable across rebuilds."""
    digest = hashlib.sha256(name.encode()).digest()
    return int.from_bytes(digest[:8], "little") & 0x7FFF_FFFF_FFFF_FFFF


# ---------------------------------------------------------------------------
# Index persistence
# ---------------------------------------------------------------------------

def _load_index():
    """Return (IdMapIndex, meta_dict) or (None, {}) if not found."""
    import turbovec
    if not TVIM_FILE.exists() or not META_FILE.exists():
        return None, {}
    try:
        index = turbovec.IdMapIndex.load(str(TVIM_FILE))
        meta  = json.loads(META_FILE.read_text())
        return index, meta
    except Exception:
        return None, {}


def _save_index(index, meta: dict) -> None:
    """Atomic write: .tvim.tmp → .tvim, .meta.json.tmp → .meta.json."""
    tmp_tvim = TVIM_FILE.with_suffix(".tvim.tmp")
    tmp_meta = META_FILE.with_suffix(".json.tmp")
    index.write(str(tmp_tvim))
    tmp_meta.write_text(json.dumps(meta))
    tmp_tvim.rename(TVIM_FILE)
    tmp_meta.rename(META_FILE)


# ---------------------------------------------------------------------------
# Memory rows
# ---------------------------------------------------------------------------

def _fetch_all() -> list[dict]:
    conn = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, type, domain, tags, body FROM memories ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _fetch_by_names(names: list[str]) -> list[dict]:
    conn = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(names))
    rows = conn.execute(
        f"SELECT name, type, domain, tags, body FROM memories WHERE name IN ({placeholders})",
        names,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _row_text(row: dict) -> str:
    """Text to embed for a memory row."""
    parts = [f"name: {row['name']}"]
    if row.get("tags"):
        parts.append(f"tags: {row['tags']}")
    if row.get("domain") and row["domain"] != "global":
        parts.append(f"domain: {row['domain']}")
    if row.get("body"):
        parts.append(row["body"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _embed(texts: list[str]) -> np.ndarray:
    from llama_index.embeddings.ollama import OllamaEmbedding
    model = OllamaEmbedding(model_name=MODEL_NAME, base_url="http://localhost:11434")
    vecs = model.get_text_embedding_batch(texts, show_progress=True)
    return np.array(vecs, dtype=np.float32)


# ---------------------------------------------------------------------------
# Full rebuild
# ---------------------------------------------------------------------------

def _full_build() -> None:
    import turbovec

    rows = _fetch_all()
    if not rows:
        print("No memories found in MEMORY.sqlite.")
        return

    print(f"  {len(rows)} memories")
    texts   = [_row_text(r) for r in rows]
    vectors = _embed(texts)

    ids  = np.array([_memory_id(r["name"]) for r in rows], dtype=np.uint64)
    meta = {
        str(_memory_id(r["name"])): {
            "name": r["name"], "type": r["type"],
            "domain": r["domain"], "tags": r["tags"],
        }
        for r in rows
    }

    index = turbovec.IdMapIndex()
    index.add_with_ids(vectors, ids)
    index.prepare()

    _save_index(index, meta)
    print(f"Written {TVIM_FILE}")
    print(f"  shape: {vectors.shape}")


# ---------------------------------------------------------------------------
# Incremental upsert (also used by memory__add hook)
# ---------------------------------------------------------------------------

def upsert_memories(names: list[str]) -> None:
    """Upsert one or more memories by name into the existing index."""
    import turbovec

    rows = _fetch_by_names(names)
    if not rows:
        print(f"No rows found for names: {names}")
        return

    index, meta = _load_index()
    if index is None:
        print("No existing index — running full rebuild.")
        _full_build()
        return

    for row in rows:
        uid = _memory_id(row["name"])
        # Remove old vector if present
        index.remove(np.uint64(uid))
        meta.pop(str(uid), None)

    texts   = [_row_text(r) for r in rows]
    vectors = _embed(texts)
    ids     = np.array([_memory_id(r["name"]) for r in rows], dtype=np.uint64)

    index.add_with_ids(vectors, ids)
    index.prepare()

    for row in rows:
        uid = _memory_id(row["name"])
        meta[str(uid)] = {
            "name": row["name"], "type": row["type"],
            "domain": row["domain"], "tags": row["tags"],
        }

    _save_index(index, meta)
    print(f"Upserted {len(rows)} memories: {[r['name'] for r in rows]}")


def remove_memory(name: str) -> None:
    """Remove a memory from the index by name."""
    import turbovec

    index, meta = _load_index()
    if index is None:
        return

    uid = _memory_id(name)
    index.remove(np.uint64(uid))
    meta.pop(str(uid), None)
    _save_index(index, meta)
    print(f"Removed '{name}' from index")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    sys.path.insert(0, str(REPO_ROOT))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name", nargs="+", metavar="SLUG",
        help="Memory name slugs to upsert (incremental). Omit for full rebuild.",
    )
    args = parser.parse_args()

    if args.name:
        print(f"Incremental upsert for: {args.name}")
        upsert_memories(args.name)
    else:
        print(f"Full rebuild from {MEMORY_DB} ...")
        _full_build()


if __name__ == "__main__":
    main()
