"""RouteModelNode — advisory per-turn model/effort recommendation, logged for grading."""
from __future__ import annotations

from langchain_learning.nodes._node_log import entry
from langchain_learning.router import route
from langchain_learning.session_state import SessionState
from src.db import route_store
from src.logger import get_logger

_log = get_logger(__name__)


class RouteModelNode:
    """Score the prompt, persist the decision, expose it as state["route_decision"].

    Runs after set_prompt_id so the decision is keyed by this turn's prompt_id.
    Before inserting, checks whether this prompt reads as a correction of the
    previous answer and flags the previous decision — the only outcome signal
    available at UPS time. Fails open: any error yields no recommendation.

    Tags: router, model-selection, effort, advisory, user-prompt-submit
    """

    def __call__(self, state: SessionState) -> dict:
        try:
            entry("route_model", state)
            prompt = state.get("prompt", "") or ""
            sid = state.get("session_id", "") or ""
            ids = list(state.get("session_prompt_ids") or [])
            tools = state.get("session_tools") or {}
            prev_count = len(tools.get(ids[-2], [])) if len(ids) >= 2 else 0

            if sid and route_store.is_correction(prompt):
                route_store.mark_correction(sid)

            d = route(prompt, turn=state.get("turn", 0), prev_tool_count=prev_count)
            route_store.record_decision(
                prompt_id=state.get("prompt_id", ""), session_id=sid, turn=state.get("turn", 0),
                prompt=prompt, tier=d.tier, model=d.model, effort=d.effort, score=d.score,
                reasons=d.reasons)
            _log.info("[route_model] tier=%s model=%s effort=%s score=%d", d.tier, d.model, d.effort, d.score)
            return {"route_decision": {"tier": d.tier, "model": d.model, "effort": d.effort,
                                       "score": d.score, "advisory": d.advisory()}}
        except Exception as exc:
            _log.warning("[route_model] failed open: %s", exc)
            return {}
