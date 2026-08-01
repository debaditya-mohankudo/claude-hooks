#!/usr/bin/env python3
"""graph_topology — derive node→chain+position from the session and task graph edge definitions.

Reads the graph structure statically (no LangGraph import needed) and returns a dict:

    {
        "load_turn":          {"chain": "user-prompt-submit", "position": 1},
        "load_active_task":   {"chain": "user-prompt-submit", "position": 2},
        ...
        "gate_check":         {"chain": "pre-tool-use",       "position": 1},
        ...
    }

Used by build_code_embeddings.py to append chain: / chain-position: tags before encoding.
"""
from __future__ import annotations

# Chain definitions: ordered list of node names per chain, matching session_graph.py.
# Conditional branches (score_tools is optional) are included at their natural position.
# Update this when graph topology changes.
_CHAINS: dict[str, list[str]] = {
    "user-prompt-submit": [
        "load_turn",
        "load_active_task",
        "load_task_history",
        "load_task_commits",
        "load_memories",
        "load_prompt_context",
        "cwd_domain_detect",
        "keyword_score",
        "combination_score",
        "memory_domain_signal",
        "apply_threshold",
        "score_tools",        # conditional — may be skipped at runtime
        "set_prompt_id",
        "log_task_events",
    ],
    "pre-tool-use": [
        "gate_check",
    ],
    "post-tool-use": [
        "log_tool_usage",
        "update_tool_keywords",
    ],
    "stop": [
        # stop chain uses noop in session_graph; real work is in stop_hook_lc.py outside graph
        "noop",
    ],
}


def get_node_topology() -> dict[str, dict[str, str | int]]:
    """Return {node_name: {chain, position}} for all known nodes."""
    topology: dict[str, dict[str, str | int]] = {}
    for chain, nodes in _CHAINS.items():
        for pos, name in enumerate(nodes, start=1):
            topology[name] = {"chain": chain, "position": pos}
    return topology


if __name__ == "__main__":
    import json
    print(json.dumps(get_node_topology(), indent=2))
