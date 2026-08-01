"""LoadTurnNode — turn counter is carried by MemorySaver checkpointer."""
from __future__ import annotations

from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)


class LoadTurnNode:
    """Turn counter is restored from MemorySaver checkpoint — no DB read needed.

    Tags: turn-counter, checkpoint, session-state
    """

    def __call__(self, state: SessionState) -> dict:
        entry("load_turn", state)
        turn = state.get("turn", 0)
        _log.info("[load_turn] session=%s turn=%d", (state.get("session_id") or "")[:8], turn)
        return {}
