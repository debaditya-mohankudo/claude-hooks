"""GateCheckNode — enforces send-gate policy for PreToolUse events."""
from __future__ import annotations

from langchain_learning.nodes._node_log import entry
from langchain_learning.retrievers import DefaultGatePolicy, GatePolicy
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)


class GateCheckNode:
    """Run gate policy against the tool call.

    Prepares a GateContext from SessionState and dispatches to the gate policy.
    The policy default (DefaultGatePolicy) wraps the GATES dict in hooks/gates.py.
    Pass a stub GatePolicy in tests to avoid hooking into the full gate machinery.

    Fail-open: returns gate_denied=False on any error.

    Tags: gate, pre-tool-use, tool-policy, gate-denied, security
    """

    def __init__(self, policy: GatePolicy | None = None) -> None:
        self._policy: GatePolicy = policy if policy is not None else DefaultGatePolicy()

    def __call__(self, state: SessionState) -> dict:
        from hooks.gates import GateContext

        tool_name  = state.get("tool_name", "")
        tool_input = state.get("tool_input") or {}
        prompt_id  = state.get("prompt_id", "")

        if not tool_name:
            _log.debug("[gate_check] no tool_name on this event — skip")
            return {"gate_denied": False, "gate_reason": ""}

        from hooks.gates import GATES
        if tool_name not in GATES:
            _log.debug("[gate_check] tool=%s not gated — skip", tool_name)
            return {"gate_denied": False, "gate_reason": ""}

        # Building current_calls / session history / recent prompt texts was
        # removed (task:84dbcf14): no gate read them, and the state fields they
        # came from were the per-session memory leak.
        ctx = GateContext(tool_name=tool_name, tool_input=tool_input, prompt_id=prompt_id)

        entry("gate_check", state, prompt_id=prompt_id[:8] if prompt_id else "?")

        deny, reason = self._policy.check(tool_name, ctx)

        if deny:
            _log.warning("[gate_check] DENY tool=%s prompt_id=%s reason=%s",
                         tool_name, prompt_id[:8] if prompt_id else "?", reason)
        else:
            _log.info("[gate_check] ALLOW tool=%s prompt_id=%s",
                      tool_name, prompt_id[:8] if prompt_id else "?")

        return {"gate_denied": deny, "gate_reason": reason}
