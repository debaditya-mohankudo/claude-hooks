"""Node registry — maps node names to callables."""
from __future__ import annotations

from langchain_learning.nodes.cwd_domain_detect import CwdDomainDetectNode
from langchain_learning.nodes.gate_check import GateCheckNode
from langchain_learning.nodes.load_active_task import LoadActiveTaskNode
from langchain_learning.nodes.load_task_history import LoadTaskHistoryNode
from langchain_learning.nodes.load_task_code import LoadTaskCodeNode
from langchain_learning.nodes.load_related_commits import LoadRelatedCommitsNode
from langchain_learning.nodes.load_memories import LoadMemoriesNode
from langchain_learning.nodes.load_turn import LoadTurnNode
from langchain_learning.nodes.log_tool_usage import LogToolUsageNode
from langchain_learning.nodes.backfill_memory_files import BackfillMemoryFilesNode
from langchain_learning.nodes.deactivate_task import DeactivateTaskNode
from langchain_learning.nodes.decision_task import DecisionTaskNode
from langchain_learning.nodes.mcp_hook_bridge import McpHookBridgeNode
from langchain_learning.nodes.noop import NoopNode
from langchain_learning.nodes.play_sound import PlaySoundNode
from langchain_learning.nodes.score_tools import ScoreToolsNode
from langchain_learning.nodes.summarize_task_context import SummarizeTaskContextNode
from langchain_learning.nodes.set_prompt_id import SetPromptIdNode

NODE_REGISTRY: dict[str, object] = {
    # UserPromptSubmit chain
    "load_turn":               LoadTurnNode,
    "load_active_task":        LoadActiveTaskNode,
    "load_task_history":       LoadTaskHistoryNode,
    "load_task_code":          LoadTaskCodeNode,
    "load_related_commits":    LoadRelatedCommitsNode,
    "load_memories":           LoadMemoriesNode,
    "cwd_domain_detect":       CwdDomainDetectNode,
    # downstream
    "score_tools":             ScoreToolsNode,
    "summarize_task_context":  SummarizeTaskContextNode,
    "set_prompt_id":           SetPromptIdNode,
    # PreToolUse chain
    "gate_check":              GateCheckNode,
    # PostToolUse chain
    "log_tool_usage":          LogToolUsageNode,
    "backfill_memory_files":   BackfillMemoryFilesNode,
    "deactivate_task":         DeactivateTaskNode,
    "decision_task":           DecisionTaskNode,
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
