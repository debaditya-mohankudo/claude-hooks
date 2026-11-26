"""LoadMemoriesNode — retrieves MEMORY.sqlite rows via combination signal scoring.

Signals per row (all cheap, SQLite-only; details in _memory_scoring.py):
  1. BM25Plus over tag words plus slug words × 3.0 (body text is not scored)
  2. Recency boost — ×1.2 if updated ≤30d, ×0.8 if ≥180d
  3. memory_links neighbours of the top seeds get a boost

A memory earns a slot by keyword match or as a linked neighbour of one that did.
Superseded and non-'general' memories are never injected (src/db/schema.injectable_sql).
Tuning: improve tags on memories that surface incorrectly (see memory_audit.sqlite).
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque

from langchain_learning.nodes._text_utils import tokenise
from langchain_learning.retrievers import CombinationSignalRetriever, MemoryRetriever
from src.logger import get_logger

_log = get_logger(__name__)

# Area carry-over: a prompt with <= _THIN_TOKENS own tokens ("yes, go ahead") borrows the
# mac areas of the last _CARRY_PROMPTS prompts' injected memories as extra query tokens.
_THIN_TOKENS = 2
_CARRY_PROMPTS = 3
_CARRY_AREAS = 2
_MAX_SESSIONS = 50

# Prompts that get no retrieval (task:beaee05c). Short ones ("commit it", "close") matched on one
# word and injected off-topic memories; the rest are written by a machine, not typed.
_SKIP_MAX_WORDS = 3
_MACHINE_PREFIXES = ("<agent-message", "<cross-session-message", "<task-notification")


def skip_reason(prompt: str) -> str:
    """'machine', 'short' or '' (retrieve). Words are counted as typed, not as scoring tokens."""
    if prompt.lstrip().startswith(_MACHINE_PREFIXES):
        return "machine"
    return "short" if len(prompt.split()) <= _SKIP_MAX_WORDS else ""


class LoadMemoriesNode:
    """Retrieve top-N memories for the current prompt via combination signal scoring.

    Scores every row in MEMORY.sqlite with BM25 over tags + slug words, times a
    recency factor. No embeddings, no external services, no domain scoping — a
    memory competes purely on keyword relevance.

    Accepts an optional MemoryRetriever at construction time; defaults to
    CombinationSignalRetriever (production backend). Pass NullMemoryRetriever or a
    custom stub in tests to avoid requiring a real SQLite fixture.

    Tags: memory, memory-injection, bm25, tag-overlap, prompt-context, MEMORY.sqlite
    """

    def __init__(self, retriever: MemoryRetriever | None = None) -> None:
        self._retriever: MemoryRetriever = retriever if retriever is not None else CombinationSignalRetriever()
        self._areas: OrderedDict[str, deque] = OrderedDict()   # session_id -> recent per-prompt area lists

    def _carried_areas(self, session_id: str) -> list[str]:
        """Up to _CARRY_AREAS distinct areas from the last prompts, newest prompt first."""
        out: list[str] = []
        for areas in reversed(self._areas.get(session_id, ())):
            for a in areas:
                if a not in out:
                    out.append(a)
        return out[:_CARRY_AREAS]

    def _remember_areas(self, session_id: str, areas: list[str]) -> None:
        dq = self._areas.setdefault(session_id, deque(maxlen=_CARRY_PROMPTS))
        dq.append(areas)
        self._areas.move_to_end(session_id)
        while len(self._areas) > _MAX_SESSIONS:
            self._areas.popitem(last=False)

    def __call__(self, state: dict) -> dict:
        prompt = state["prompt"]
        tokens = set(tokenise(prompt.lower()))
        session_id = state.get("session_id", "")

        reason = skip_reason(prompt)
        if reason:
            # Carry-over history is left alone, so a skipped prompt does not age out earlier areas.
            try:
                from langchain_learning.nodes._skill_memories import note_prompt_injected
                note_prompt_injected(session_id, [])
            except Exception as exc:
                _log.warning("skill memories prompt tracking failed: %s", exc)
            try:
                from langchain_learning.nodes import _memory_audit
                _memory_audit.record(session_id, prompt, tokens, [], [], [], skipped=reason)
            except Exception as exc:
                _log.warning("memory audit failed: %s", exc)
            _log.info("[load_memories] skipped=%s", reason)
            return {"memories": [], "keywords": list(tokens)}

        carried = self._carried_areas(session_id) if len(tokens) <= _THIN_TOKENS else []
        tokens |= set(carried)

        t0 = time.monotonic()
        try:
            memories = self._retriever.retrieve(tokens)
        except Exception as exc:
            _log.error("[load_memories] retriever error: %s", exc)
            return {"memories": [], "keywords": list(tokens)}
        retrieve_ms = (time.monotonic() - t0) * 1000

        names_out = [m.get("name", "?") for m in memories]
        try:
            from langchain_learning.nodes._skill_memories import note_prompt_injected
            note_prompt_injected(session_id, names_out)
        except Exception as exc:
            _log.warning("skill memories prompt tracking failed: %s", exc)
        try:
            from langchain_learning.nodes._memory_scoring import area_tokens
            self._remember_areas(session_id, area_tokens(memories))
        except Exception as exc:
            _log.warning("area carry-over update failed: %s", exc)
        try:
            from langchain_learning.nodes import _memory_audit
            _memory_audit.record(session_id, prompt, tokens, carried,
                                 getattr(self._retriever, "last_trace", []), names_out)
        except Exception as exc:
            _log.warning("memory audit failed: %s", exc)
        _log.info(
            "[load_memories] mode=combination returned=%d keywords=%d retrieve_ms=%.0f names=%s",
            len(memories), len(tokens), retrieve_ms, names_out,
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
