"""Node registry — maps node names to callables."""
from __future__ import annotations

from langchain_learning.nodes.gate_check import GateCheckNode
from langchain_learning.nodes.load_memories import LoadMemoriesNode
from langchain_learning.nodes.load_turn import LoadTurnNode
from langchain_learning.nodes.log_tool_usage import LogToolUsageNode
from langchain_learning.nodes.backfill_memory_files import BackfillMemoryFilesNode
from langchain_learning.nodes.mcp_hook_bridge import McpHookBridgeNode
from langchain_learning.nodes.noop import NoopNode
from langchain_learning.nodes.play_sound import PlaySoundNode
from langchain_learning.nodes.score_tools import ScoreToolsNode
from langchain_learning.nodes.set_prompt_id import SetPromptIdNode

# load_active_task, load_task_code, load_related_commits, summarize_task_context,
# deactivate_task, decision_task removed (task:882d67fa) — task-framework owns
# active-task context outright now; see session_graph.py's UPS/PostToolUse chain
# comments for the full rationale.
NODE_REGISTRY: dict[str, object] = {
    # UserPromptSubmit chain
    "load_turn":               LoadTurnNode,
    "load_memories":           LoadMemoriesNode,
    # downstream
    "score_tools":             ScoreToolsNode,
    "set_prompt_id":           SetPromptIdNode,
    # PreToolUse chain
    "gate_check":              GateCheckNode,
    # PostToolUse chain
    "log_tool_usage":          LogToolUsageNode,
    "backfill_memory_files":   BackfillMemoryFilesNode,
    "mcp_hook_bridge":         McpHookBridgeNode,
    # Stop chain
    "play_sound":              PlaySoundNode,
    # Fallback
    "noop":                    NoopNode,
}


def get_node(name: str):
    """Return a callable node by name. Classes are instantiated; other callables returned as-is."""
    node = NODE_REGISTRY[name]
    if not callable(node):
        raise TypeError(f"Registry entry {name!r} is not callable, got {type(node).__name__}")
    return node() if isinstance(node, type) else node
