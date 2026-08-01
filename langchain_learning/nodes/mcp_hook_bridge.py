"""McpHookBridgeNode — generic PostToolUse bridge for the __hook__ convention.

Any MCP tool that returns a __hook__ key in its response gets automatic
additionalSystemPrompt injection — no per-integration PostToolUse node needed.

Convention:
    tool returns: {"__hook__": {"additionalSystemPrompt": "..."}, ...rest}
    bridge returns: {"pending_hook_output": {"additionalSystemPrompt": "..."}}

This is a factory pattern: _post_tool_route checks for __hook__ presence and
routes here. McpHookBridgeNode is the generic product — it pipes the payload
through without caring which tool produced it.

Tags: mcp, post-tool-use, hook, bridge, generic, additionalSystemPrompt
"""
from __future__ import annotations

from langchain_learning.nodes._json_utils import extract_tool_result_json
from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)


class McpHookBridgeNode:
    """Generic PostToolUse bridge — pipes __hook__ from any MCP tool result
    into pending_hook_output so Claude Code injects it as additionalSystemPrompt.

    Tags: mcp, hook, bridge, generic, posttooluse
    """

    def __call__(self, state: SessionState) -> dict:
        entry("mcp_hook_bridge", state)

        tool_name = state.get("tool_name", "")
        tool_result = state.get("tool_result") or {}
        session_id = str(state.get("session_id", ""))[:8]

        result = extract_tool_result_json(tool_result)
        hook = result.get("__hook__") or {}

        if not hook:
            _log.debug("[mcp_hook_bridge] session=%s tool=%s — no __hook__ in result", session_id, tool_name)
            return {}

        _log.info(
            "[mcp_hook_bridge] session=%s tool=%s — injecting hook keys=%s",
            session_id, tool_name, list(hook.keys()),
        )
        return {"pending_hook_output": hook}
