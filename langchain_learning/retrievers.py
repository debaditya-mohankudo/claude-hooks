"""Public Protocol contract for the memory retrieval layer.

(GatePolicy / DefaultGatePolicy were removed in task:31fb711d: nothing but the
deleted gate_check node used them; the dispatcher calls hooks.gates.check directly.)

These interfaces are the joints in the memory injection pipe:
  prompt → tokenise → MemoryRetriever → LoadMemoriesNode → additionalContext

Concrete implementations live in nodes/ and hooks/ and import from here.
This file must never import from nodes/ or hooks/ — the dependency arrow is:
  nodes/ → retrievers.py, never the reverse.

Design principle: Callable classes with minimal structural contracts (ACME POC Principle 13).
Protocols are structural — no inheritance required to satisfy them.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.logger import get_logger

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# MemoryRetriever
# ---------------------------------------------------------------------------

@runtime_checkable
class MemoryRetriever(Protocol):
    """Score and return relevant memories for a set of prompt tokens.

    Implementations:
      CombinationSignalRetriever — wraps score_memories() (nodes/_memory_scoring.py)
      NullMemoryRetriever        — returns [] (testing / disabled)
    """

    def retrieve(
        self,
        tokens: set[str],
        top_n: int | None = None,
    ) -> list[dict]: ...


# ---------------------------------------------------------------------------
# Null implementations — safe defaults for testing and opt-out
# ---------------------------------------------------------------------------

class NullMemoryRetriever:
    """No-op retriever — always returns empty. Satisfies MemoryRetriever Protocol."""

    def retrieve(
        self, tokens: set[str], top_n: int | None = None
    ) -> list[dict]:
        return []


# ---------------------------------------------------------------------------
# Default implementations — live backends, used by nodes in production
# ---------------------------------------------------------------------------

class CombinationSignalRetriever:
    """Scores MEMORY.sqlite rows via BM25 over tags + slug words, recency and links.

    Wraps score_memories() from nodes/_memory_scoring.py.
    Reads through the process-wide memory_connection(), not a per-call open.
    Satisfies MemoryRetriever Protocol.

    `last_trace` holds the top candidates (name, score, hits) of the latest
    retrieve() call, for the memory audit log.
    """

    last_trace: list[dict] = []

    def retrieve(
        self,
        tokens: set[str],
        top_n: int | None = None,
    ) -> list[dict]:
        import sqlite3
        from langchain_learning.config import config as _cfg
        from langchain_learning.nodes._memory_scoring import score_memories
        from langchain_learning.memory_conn import memory_connection

        if not _cfg.memory_db.exists():
            _log.debug("[CombinationSignalRetriever] memory_db not found: %s", _cfg.memory_db)
            return []
        # Shared process connection, not a fresh open_db: an encrypted open costs
        # ~300 ms of key derivation per call (task:bc39e90e).
        try:
            with memory_connection(_cfg.memory_db, row_factory=sqlite3.Row) as conn:
                trace: list[dict] = []
                results = score_memories(tokens, conn, top_n=top_n, trace=trace)
            self.last_trace = trace
            _log.debug("[CombinationSignalRetriever] scored=%d tokens=%d",
                       len(results), len(tokens))
            return results
        except Exception as exc:
            _log.error("[CombinationSignalRetriever] score_memories error: %s", exc)
            return []

