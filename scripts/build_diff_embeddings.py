#!/usr/bin/env python3
"""Build TurboVec RAG embeddings for git diff history (per-hunk strategy).

Each @@ hunk block becomes one document, enriched with a metadata header
(file, commit hash, author, date) prepended before embedding.

Incremental: already-indexed commits are skipped. Only new commits since the
last run are parsed and embedded.

Output:
  .diff_embeddings.tvim      — TurboVec IdMapIndex (gitignored)
  .diff_embeddings.meta.json — chunk metadata sidecar (gitignored)

Usage:
    uv run python scripts/build_diff_embeddings.py              # full history
    uv run python scripts/build_diff_embeddings.py --since HEAD~50
    uv run python scripts/build_diff_embeddings.py --repo /path/to/other/repo
    uv run python scripts/build_diff_embeddings.py --max-commits 200
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO_ROOT  = Path(__file__).resolve().parent.parent
TVIM_FILE  = REPO_ROOT / ".diff_embeddings.tvim"
META_FILE  = REPO_ROOT / ".diff_embeddings.meta.json"
MODEL_NAME = "nomic-embed-text"
BATCH_SIZE = 32
MAX_HUNK_LINES = 200
SNIPPET_LINES  = 30
_INDEXED_KEY   = "__indexed_commits__"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class DiffHunk:
    commit_hash: str
    author: str
    date: str
    file_path: str
    hunk_index: int       # 0-based within (commit, file)
    hunk_header: str      # the @@ ... @@ line (with optional function context)
    hunk_text: str        # full diff lines for this hunk


# ---------------------------------------------------------------------------
# Stable chunk IDs
# ---------------------------------------------------------------------------

def _chunk_id(commit_hash: str, file_path: str, hunk_index: int) -> int:
    key = f"{commit_hash}::{file_path}::{hunk_index}"
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "little") & 0x7FFF_FFFF_FFFF_FFFF


# ---------------------------------------------------------------------------
# Git diff parser
# ---------------------------------------------------------------------------

def _run_git_log(repo: Path, since: str | None, max_commits: int | None) -> str:
    cmd = ["git", "log", "--patch", "--no-merges", "--unified=3",
           "--diff-filter=AM"]  # only Added/Modified files
    if since:
        cmd.append(f"{since}..HEAD")
    if max_commits:
        cmd += ["-n", str(max_commits)]
    result = subprocess.run(cmd, cwd=repo, capture_output=True, timeout=120)
    return result.stdout.decode("utf-8", errors="replace")


def _parse_hunks(log_output: str) -> list[DiffHunk]:
    """State-machine parser: commit → file → hunk."""
    hunks: list[DiffHunk] = []

    commit_hash = ""
    author      = ""
    date        = ""
    file_path   = ""
    hunk_index  = 0
    hunk_header = ""
    hunk_lines: list[str] = []

    def _flush_hunk():
        if hunk_header and hunk_lines and commit_hash and file_path:
            text = "\n".join(hunk_lines[:MAX_HUNK_LINES])
            if len(hunk_lines) > MAX_HUNK_LINES:
                text += "\n# ... truncated"
            hunks.append(DiffHunk(
                commit_hash=commit_hash,
                author=author,
                date=date,
                file_path=file_path,
                hunk_index=hunk_index,
                hunk_header=hunk_header,
                hunk_text=text,
            ))

    for line in log_output.splitlines():
        if line.startswith("commit "):
            _flush_hunk()
            hunk_lines = []
            hunk_header = ""
            file_path = ""
            hunk_index = 0
            commit_hash = line[7:].strip()
            author = ""
            date = ""

        elif line.startswith("Author:"):
            author = line[7:].strip()

        elif line.startswith("Date:"):
            date = line[5:].strip()

        elif line.startswith("diff --git "):
            _flush_hunk()
            hunk_lines = []
            hunk_header = ""
            hunk_index = 0
            # extract b/ path
            parts = line.split(" b/", 1)
            file_path = parts[1].strip() if len(parts) == 2 else ""

        elif line.startswith("Binary files"):
            # skip binary diffs
            file_path = ""
            hunk_header = ""
            hunk_lines = []

        elif line.startswith("@@"):
            _flush_hunk()
            hunk_lines = []
            hunk_header = line
            hunk_index += 1 if hunk_header else 0

        elif hunk_header:
            hunk_lines.append(line)

    _flush_hunk()
    return hunks


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def _enrich(hunk: DiffHunk) -> str:
    """Prepend metadata header to hunk text before embedding."""
    header = (
        f"File: {hunk.file_path} | Commit: {hunk.commit_hash[:12]} "
        f"| Author: {hunk.author} | Date: {hunk.date}"
    )
    return f"{header}\n{hunk.hunk_header}\n{hunk.hunk_text}"


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _embed(texts: list[str]) -> np.ndarray:
    from llama_index.embeddings.ollama import OllamaEmbedding
    model = OllamaEmbedding(model_name=MODEL_NAME)
    all_vecs = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i: i + BATCH_SIZE]
        vecs  = model.get_text_embedding_batch(batch, show_progress=True)
        all_vecs.extend(vecs)
    return np.array(all_vecs, dtype=np.float32)


# ---------------------------------------------------------------------------
# Index persistence (reuses rag_core API)
# ---------------------------------------------------------------------------

def _load_index():
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from tools.rag_core import load_index
    return load_index(TVIM_FILE, META_FILE)


def _save_index(index, meta: dict) -> None:
    from tools.rag_core import save_index
    save_index(index, meta, TVIM_FILE, META_FILE)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(repo: Path, since: str | None, max_commits: int | None) -> None:
    import turbovec

    print(f"Parsing git log from {repo} ...")
    log_output = _run_git_log(repo, since, max_commits)
    if not log_output.strip():
        print("No commits found — nothing to index.")
        return

    all_hunks = _parse_hunks(log_output)
    print(f"  {len(all_hunks)} hunks parsed from git log")

    # Load existing index to skip already-indexed commits
    index, meta = _load_index()
    indexed_commits: set[str] = set(meta.get(_INDEXED_KEY, []))

    new_hunks = [h for h in all_hunks if h.commit_hash not in indexed_commits]
    if not new_hunks:
        print("All commits already indexed — nothing to do.")
        return
    print(f"  {len(new_hunks)} new hunks from {len({h.commit_hash for h in new_hunks})} commits")

    texts   = [_enrich(h) for h in new_hunks]
    vectors = _embed(texts)

    ids = np.array([_chunk_id(h.commit_hash, h.file_path, h.hunk_index) for h in new_hunks],
                   dtype=np.uint64)

    if index is None:
        index = turbovec.IdMapIndex()

    index.add_with_ids(vectors, ids)
    index.prepare()

    for hunk, cid in zip(new_hunks, ids):
        snippet = "\n".join(hunk.hunk_text.splitlines()[:SNIPPET_LINES])
        meta[str(int(cid))] = {
            "commit_hash":  hunk.commit_hash,
            "author":       hunk.author,
            "date":         hunk.date,
            "file":         hunk.file_path,
            "hunk_index":   hunk.hunk_index,
            "hunk_header":  hunk.hunk_header,
            "snippet":      snippet,
        }

    indexed_commits.update(h.commit_hash for h in new_hunks)
    meta[_INDEXED_KEY] = list(indexed_commits)

    _save_index(index, meta)
    print(f"Written {TVIM_FILE.name}  ({vectors.shape[0]} vectors, dim={vectors.shape[1]})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=str(REPO_ROOT),
                        help="Absolute path to the git repo to index (default: claude-hooks)")
    parser.add_argument("--since", default=None,
                        help="Git ref to start from, e.g. HEAD~50 or a commit hash")
    parser.add_argument("--max-commits", type=int, default=None,
                        help="Limit number of commits to process")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"Error: {repo} is not a git repository.", file=sys.stderr)
        sys.exit(1)

    build(repo, since=args.since, max_commits=args.max_commits)


if __name__ == "__main__":
    main()
