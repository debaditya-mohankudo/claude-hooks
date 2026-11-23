"""SetPromptIdNode — generates a UUID for this turn."""
from __future__ import annotations

import uuid

from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)


class SetPromptIdNode:
    """Generate a fresh prompt_id UUID for this UserPromptSubmit turn.

    prompt_id flows via LangGraph checkpoint state to the PreToolUse gates.
    This node used to also reset prompt_tools and append to session_prompt_ids,
    session_tools and session_prompt_texts; those fields were removed
    (task:84dbcf14) because nothing read them and they grew for the whole
    session.

    Tags: prompt-id, uuid, session-state, user-prompt-submit, turn-tracking
    """

    def __call__(self, state: SessionState) -> dict:
        entry("set_prompt_id", state)
        prompt_id = str(uuid.uuid4())
        turn = state.get("turn", 0) + 1
        _log.info("[set_prompt_id] prompt_id=%s turn=%d", prompt_id[:8], turn)
        return {"prompt_id": prompt_id, "turn": turn}
