"""Shared JSON extraction utility for PostToolUse nodes.

MCP tool results arrive in one of three shapes:
  1. MCP content wrapper: {"content": [{"text": "<json string>"}]}
  2. Raw dict (direct return from tool)
  3. JSON string

Tags: mcp, tool-result, json, utils
"""
from __future__ import annotations

import json


def extract_tool_result_json(tool_result: dict) -> dict:
    """Extract JSON payload from an MCP tool_result in any of its wire shapes."""
    if isinstance(tool_result, dict):
        # Shape 1: MCP content wrapper
        if "content" in tool_result and isinstance(tool_result.get("content"), list):
            try:
                text = tool_result["content"][0].get("text", "")
                return json.loads(text)
            except Exception:
                pass
        # Shape 2: already a plain dict
        return tool_result
    # Shape 3: JSON string
    try:
        return json.loads(str(tool_result))
    except Exception:
        return {}
