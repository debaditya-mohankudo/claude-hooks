"""LoadTaskCodeNode — semantic search over repo's TurboVec index (.code_embeddings.tvim)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger
from src.tools.rag_core import load_index, query_index

try:
    from llama_index.embeddings.ollama import OllamaEmbedding
    _RAG_AVAILABLE = True
except Exception:
    _RAG_AVAILABLE = False
    OllamaEmbedding = None  # type: ignore[assignment,misc]

_log = get_logger(__name__)

_REPO_ROOT   = Path(__file__).resolve().parents[2]
_TVIM_PATH   = _REPO_ROOT / ".code_embeddings.tvim"
_META_PATH   = _REPO_ROOT / ".code_embeddings.meta.json"
_TOP_K       = 3
_EMBED_MODEL = "nomic-embed-text"


def _query_tvim(query: str, k: int, tvim_path: Path, meta_path: Path) -> list[dict]:
    """Embed query with Ollama nomic-embed-text, search TurboVec index, return top-k."""

    index, meta = load_index(tvim_path, meta_path)
    if index is None:
        _log.warning("[load_task_code] .code_embeddings.tvim not found or failed to load")
        return []

    embed_model = OllamaEmbedding(model_name=_EMBED_MODEL)
    q_vec = np.array([embed_model.get_text_embedding(query)], dtype=np.float32)

    results = query_index(index, meta, q_vec, k=k)
    return results


class LoadTaskCodeNode:
    """Semantic search over repo's TurboVec index — returns top-3 relevant code symbols.

    Resolves .code_embeddings.tvim from state['cwd'] (the Claude session's working
    directory) so cross-repo tasks search the correct project's index. Falls back to
    the claude-hooks root index if cwd is unset or no index exists there.

    Embeds active_task_title via Ollama nomic-embed-text. Returns per-symbol hits
    (module, file, name, kind, line) as task_rag_chunks, rendered as ## Relevant code.

    Falls back to empty list if index not found or Ollama unavailable.

    Tags: task-code, rag, turbovec, semantic, embeddings, task-context, cwd
    """

    def __call__(self, state: SessionState) -> dict:
        entry("load_task_code", state)

        task_title = state.get("active_task_title", "")
        task_id    = state.get("active_task_id", "")

        if not task_id or not task_title:
            return {"task_rag_chunks": []}

        cwd = state.get("cwd", "")
        tvim = Path(cwd) / ".code_embeddings.tvim" if cwd else _TVIM_PATH
        meta = Path(cwd) / ".code_embeddings.meta.json" if cwd else _META_PATH

        if not tvim.exists():
            _log.warning("[load_task_code] index not found: %s", tvim)
            return {"task_rag_chunks": []}

        try:
            chunks = _query_tvim(task_title, _TOP_K, tvim, meta)
        except Exception as exc:
            _log.error("[load_task_code] TurboVec query failed: %s", exc)
            return {"task_rag_chunks": []}

        _log.info("[load_task_code] task=%s query=%r chunks=%d symbols=%s",
                  task_id[:8], task_title[:40], len(chunks),
                  [c.get("name", "?") for c in chunks])
        return {"task_rag_chunks": chunks}
