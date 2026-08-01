"""SetPromptIdNode — generates a UUID for this turn and resets prompt tracking state."""
from __future__ import annotations

import uuid
from collections import OrderedDict

from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)


class SetPromptIdNode:
    """Generate a fresh prompt_id UUID for this UserPromptSubmit turn.

    Resets prompt_tools to [] so gate_check only sees tools called this prompt.
    Appends prompt_id to session_prompt_ids and initialises its entry in session_tools.
    prompt_id and prompt_tools flow entirely via LangGraph checkpoint state.

    Tags: prompt-id, uuid, session-state, user-prompt-submit, turn-tracking
    """

    def __call__(self, state: SessionState) -> dict:
        entry("set_prompt_id", state)
        prompt_id = str(uuid.uuid4())

        session_prompt_ids = list(state.get("session_prompt_ids") or [])
        session_prompt_ids.append(prompt_id)

        session_tools: OrderedDict[str, list[str]] = OrderedDict(state.get("session_tools") or {})
        session_tools[prompt_id] = []

        session_prompt_texts: dict[str, str] = dict(state.get("session_prompt_texts") or {})
        session_prompt_texts[prompt_id] = state.get("prompt", "")

        _log.info("[set_prompt_id] prompt_id=%s session_depth=%d", prompt_id[:8], len(session_prompt_ids))
        return {
            "prompt_id": prompt_id,
            "prompt_tools": [],
            "session_prompt_ids": session_prompt_ids,
            "session_tools": session_tools,
            "session_prompt_texts": session_prompt_texts,
            "turn": state.get("turn", 0) + 1,
        }
