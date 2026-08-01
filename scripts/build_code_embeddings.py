#!/usr/bin/env python3
"""Build TurboVec RAG embeddings for the full claude-hooks repo (Python source + docs).

Chunks each .py file by function/class body (AST-based) and each .md file in
docs/ by ## section. Each chunk maps to a stable uint64 ID derived from
sha256(file+name) so incremental upserts work without a full rebuild.

Output:
  .code_embeddings.tvim      — TurboVec IdMapIndex (gitignored)
  .code_embeddings.meta.json — chunk metadata sidecar (gitignored)

Usage:
    uv run python scripts/build_code_embeddings.py              # full rebuild
    uv run python scripts/build_code_embeddings.py --files langchain_learning/nodes/load_memories.py docs/arch/graph_pipeline.md
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO_ROOT  = Path(__file__).resolve().parent.parent
SKIP_DIRS  = {".venv", "__pycache__", ".git", "node_modules", "tests"}
DOCS_DIRS  = ["docs"]
TVIM_FILE  = REPO_ROOT / ".code_embeddings.tvim"
META_FILE  = REPO_ROOT / ".code_embeddings.meta.json"
MODEL_NAME = "nomic-embed-text"  # Ollama local model, 768-dim


# ---------------------------------------------------------------------------
# Stable chunk IDs
# ---------------------------------------------------------------------------

def _chunk_id(file: str, name: str) -> int:
    """Deterministic uint64 from (file, name) — stable across rebuilds."""
    digest = hashlib.sha256(f"{file}::{name}".encode()).digest()
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
# Git SHA
# ---------------------------------------------------------------------------

def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Chunk extraction
# ---------------------------------------------------------------------------

def _collect_py_files() -> list[Path]:
    files = []
    for p in sorted(REPO_ROOT.rglob("*.py")):
        if any(skip in p.parts for skip in SKIP_DIRS):
            continue
        files.append(p)
    return files


def _collect_docs() -> list[Path]:
    docs = []
    for d in DOCS_DIRS:
        for p in sorted((REPO_ROOT / d).rglob("*.md")):
            docs.append(p)
    return docs


def _extract_py_chunks(path: Path) -> list[dict]:
    src   = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []

    rel    = str(path.relative_to(REPO_ROOT))
    module = rel.replace("/", ".").removesuffix(".py")
    chunks = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chunks.append({
                "id":     _chunk_id(rel, node.name),
                "module": module, "file": rel,
                "name":   node.name, "kind": "function", "line": node.lineno,
                "text":   "\n".join(lines[node.lineno - 1: node.end_lineno]),
            })
        elif isinstance(node, ast.ClassDef):
            chunks.append({
                "id":     _chunk_id(rel, node.name),
                "module": module, "file": rel,
                "name":   node.name, "kind": "class", "line": node.lineno,
                "text":   "\n".join(lines[node.lineno - 1: node.end_lineno]),
            })

    return chunks


def _parse_frontmatter(src: str) -> tuple[str, str]:
    """Return (tags_line, body_without_frontmatter). tags_line is empty if no frontmatter."""
    import re
    m = re.match(r"^---\n(.*?)\n---\n", src, re.DOTALL)
    if not m:
        return "", src
    fm = m.group(1)
    tags_match = re.search(r"^tags:\s*(.+)$", fm, re.MULTILINE)
    tags = tags_match.group(1).strip() if tags_match else ""
    return tags, src[m.end():]


def _extract_md_chunks(path: Path) -> list[dict]:
    import re
    src    = path.read_text(encoding="utf-8", errors="replace")
    rel    = str(path.relative_to(REPO_ROOT))
    module = rel.replace("/", ".").removesuffix(".md")

    tags, body = _parse_frontmatter(src)

    # Strip fenced code blocks so ## inside examples don't create phantom sections
    body_no_fences = re.sub(r"(?ms)^```.*?^```", lambda m: "\n" * m.group().count("\n"), body)
    sections    = re.split(r"(?m)^(?=## )", body_no_fences)
    chunks      = []
    line_cursor = 1 + src[: len(src) - len(body)].count("\n")
    for section in sections:
        if not section.strip():
            line_cursor += section.count("\n")
            continue
        heading_match = re.match(r"^##+ (.+)", section)
        name = heading_match.group(1).strip() if heading_match else module
        text = (f"Tags: {tags}\n" + section.rstrip()) if tags else section.rstrip()
        chunks.append({
            "id":     _chunk_id(rel, name),
            "module": module, "file": rel,
            "name":   name, "kind": "section", "line": line_cursor,
            "text":   text,
        })
        line_cursor += section.count("\n")
    return chunks


def build_chunks() -> list[dict]:
    chunks = []
    for path in _collect_py_files():
        chunks.extend(_extract_py_chunks(path))
    for path in _collect_docs():
        chunks.extend(_extract_md_chunks(path))
    return chunks


def chunks_for_files(rel_paths: list[str]) -> list[dict]:
    """Return chunks for a specific subset of repo-relative file paths."""
    chunks = []
    for rel in rel_paths:
        path = REPO_ROOT / rel
        if not path.exists():
            print(f"  [skip] not found: {rel}")
            continue
        if path.suffix == ".py":
            chunks.extend(_extract_py_chunks(path))
        elif path.suffix == ".md":
            chunks.extend(_extract_md_chunks(path))
        else:
            print(f"  [skip] unsupported file type: {rel}")
    return chunks


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _embed(texts: list[str]) -> np.ndarray:
    from llama_index.embeddings.ollama import OllamaEmbedding
    model = OllamaEmbedding(model_name=MODEL_NAME)
    vecs = model.get_text_embedding_batch(texts, show_progress=True)
    return np.array(vecs, dtype=np.float32)


# ---------------------------------------------------------------------------
# Full rebuild
# ---------------------------------------------------------------------------

def _full_build() -> None:
    import turbovec

    py_files  = _collect_py_files()
    doc_files = _collect_docs()
    chunks    = build_chunks()
    print(f"  {len(chunks)} chunks ({len(py_files)} .py files + {len(doc_files)} .md files)")

    texts   = [c["text"] for c in chunks]
    vectors = _embed(texts)

    ids  = np.array([c["id"] for c in chunks], dtype=np.uint64)
    meta = {str(c["id"]): {k: v for k, v in c.items() if k not in ("text", "id")} for c in chunks}
    # store commit in meta under special key
    meta["__commit__"] = _git_sha()

    index = turbovec.IdMapIndex()
    index.add_with_ids(vectors, ids)
    index.prepare()

    _save_index(index, meta)

    sha = meta["__commit__"]
    print(f"Written {TVIM_FILE.relative_to(REPO_ROOT)}")
    print(f"  shape: {vectors.shape}, commit: {sha[:12]}")


# ---------------------------------------------------------------------------
# Incremental upsert
# ---------------------------------------------------------------------------

def _incremental_upsert(rel_paths: list[str]) -> None:
    import turbovec

    index, meta = _load_index()
    if index is None:
        print("No existing index — falling back to full rebuild.")
        _full_build()
        return

    new_chunks = chunks_for_files(rel_paths)
    if not new_chunks:
        print("No chunks extracted from given files.")
        return

    # Determine which IDs are already in index (by file overlap)
    files_set  = {c["file"] for c in new_chunks}
    old_ids    = [
        int(cid) for cid, info in meta.items()
        if cid != "__commit__" and info.get("file") in files_set
    ]

    # Remove old vectors
    for old_id in old_ids:
        index.remove(np.uint64(old_id))
        meta.pop(str(old_id), None)

    texts   = [c["text"] for c in new_chunks]
    vectors = _embed(texts)
    ids     = np.array([c["id"] for c in new_chunks], dtype=np.uint64)

    index.add_with_ids(vectors, ids)
    index.prepare()

    for c in new_chunks:
        meta[str(c["id"])] = {k: v for k, v in c.items() if k not in ("text", "id")}

    meta["__commit__"] = _git_sha()
    _save_index(index, meta)

    sha = meta["__commit__"]
    print(f"Upserted {len(new_chunks)} chunks (removed {len(old_ids)} old) — commit: {sha[:12]}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--files", nargs="+", metavar="PATH",
        help="Repo-relative .md file paths to upsert (incremental). Omit for full rebuild.",
    )
    args = parser.parse_args()

    if args.files:
        print(f"Incremental upsert for: {args.files}")
        _incremental_upsert(args.files)
    else:
        print(f"Scanning {REPO_ROOT} ...")
        _full_build()


if __name__ == "__main__":
    main()
