"""LoadMemoriesNode — retrieves MEMORY.sqlite rows via combination signal scoring.

Signals per row (all cheap, SQLite-only):
  1. Domain weight   — project domain: 2.0 | global: 0.5 | other: skip
  2. Tag overlap     — Jaccard(prompt_tokens ∩ tag_tokens) × 3.0  (hand-authored, high signal)
  3. Body overlap    — Jaccard(prompt_tokens ∩ body_tokens) × 1.0
  4. Recency boost   — ×1.2 if updated ≤30d, ×0.8 if ≥180d

Global memories are not auto-included — they must earn a slot via keyword overlap.
Tuning: improve tags on memories that surface incorrectly (visible in sqlite logs).
"""
from __future__ import annotations

from langchain_learning.config import config as _cfg
from langchain_learning.nodes._node_log import entry
from langchain_learning.nodes._text_utils import tokenise
from langchain_learning.retrievers import CombinationSignalRetriever, MemoryRetriever
from langchain_learning.session_state import SessionState
from src.config import config as _src_cfg
from src.logger import get_logger

_log = get_logger(__name__)


class LoadMemoriesNode:
    """Retrieve top-N memories for the current prompt via combination signal scoring.

    Scores every row in MEMORY.sqlite by domain weight + tag overlap + body overlap
    + recency. No embeddings, no external services. Global domain competes on keyword
    relevance — not automatically included.

    Accepts an optional MemoryRetriever at construction time; defaults to
    CombinationSignalRetriever (production backend). Pass NullMemoryRetriever or a
    custom stub in tests to avoid requiring a real SQLite fixture.

    Tags: memory, memory-injection, combination-signal, bm25, tag-overlap, prompt-context, MEMORY.sqlite
    """

    def __init__(self, retriever: MemoryRetriever | None = None) -> None:
        self._retriever: MemoryRetriever = retriever if retriever is not None else CombinationSignalRetriever()

    def __call__(self, state: SessionState) -> dict:
        entry("load_memories", state, prompt_len=len(state.get("prompt", "")))

        prompt = state["prompt"]
        tokens = set(tokenise(prompt.lower()))

        cwd = state.get("cwd", "")
        project_domain = next(
            (domain for key, domain in _src_cfg.cwd_domain_map.items() if key.lower() in cwd.lower()),
            None,
        )

        try:
            memories = self._retriever.retrieve(tokens, project_domain)
        except Exception as exc:
            _log.error("[load_memories] retriever error: %s", exc)
            return {"memories": [], "keywords": list(tokens)}

        names_out = [m.get("name", "?") for m in memories]
        _log.info(
            "[load_memories] mode=combination returned=%d keywords=%d project_domain=%s names=%s",
            len(memories), len(tokens), project_domain, names_out,
        )
        try:
            from hooks.server_memory import record_memories
            record_memories(state.get("session_id", ""), names_out)
        except Exception as exc:
            _log.warning("record_memories failed (tool-hint tracking degraded): %s", exc)

        try:
            from langchain_learning.nodes._memory_scoring import record_memory_hits
            record_memory_hits(names_out)
        except Exception as exc:
            _log.warning("record_memory_hits failed (hit-count tracking degraded): %s", exc)

        return {"memories": memories, "keywords": list(tokens)}
