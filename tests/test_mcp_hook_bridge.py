"""Tests for McpHookBridgeNode and extract_tool_result_json."""
from __future__ import annotations

import json

from langchain_learning.nodes._json_utils import extract_tool_result_json
from langchain_learning.nodes.mcp_hook_bridge import McpHookBridgeNode


# ── extract_tool_result_json ───────────────────────────────────────────────────

def test_extract_mcp_content_wrapper():
    payload = {"status": "done", "__hook__": {"additionalSystemPrompt": "hi"}}
    tool_result = {"content": [{"text": json.dumps(payload)}]}
    assert extract_tool_result_json(tool_result) == payload


def test_extract_raw_dict():
    payload = {"status": "continue", "__hook__": {}}
    assert extract_tool_result_json(payload) == payload


def test_extract_json_string():
    payload = {"foo": "bar"}
    assert extract_tool_result_json(json.dumps(payload)) == payload


def test_extract_empty_dict():
    assert extract_tool_result_json({}) == {}


def test_extract_garbage_string():
    assert extract_tool_result_json("not json!!!") == {}


def test_extract_content_wrapper_bad_json():
    tool_result = {"content": [{"text": "not-json"}]}
    # falls through to raw dict path — returns the wrapper itself
    result = extract_tool_result_json(tool_result)
    assert "content" in result


# ── McpHookBridgeNode ──────────────────────────────────────────────────────────

def _state(tool_name: str, tool_result: dict) -> dict:
    return {
        "session_id": "sess-test-abcd",
        "tool_name": tool_name,
        "tool_result": tool_result,
    }


def _wrap(payload: dict) -> dict:
    return {"content": [{"text": json.dumps(payload)}]}


def test_bridge_injects_additional_system_prompt():
    hook = {"additionalSystemPrompt": "## Next step\nCall submit_report again."}
    node = McpHookBridgeNode()
    result = node(_state("some_mcp__tool", _wrap({"status": "continue", "__hook__": hook})))
    assert result == {"pending_hook_output": hook}


def test_bridge_passes_through_full_hook_dict():
    hook = {"additionalSystemPrompt": "...", "suppressOutput": True}
    node = McpHookBridgeNode()
    result = node(_state("tool_a", _wrap({"__hook__": hook})))
    assert result["pending_hook_output"] == hook


def test_bridge_returns_empty_when_no_hook():
    node = McpHookBridgeNode()
    result = node(_state("tasks__list", _wrap({"tasks": []})))
    assert result == {}


def test_bridge_returns_empty_when_hook_is_empty_dict():
    node = McpHookBridgeNode()
    result = node(_state("some_tool", _wrap({"__hook__": {}})))
    assert result == {}


def test_bridge_returns_empty_on_empty_tool_result():
    node = McpHookBridgeNode()
    result = node(_state("some_tool", {}))
    assert result == {}


def test_bridge_works_with_raw_dict_result():
    hook = {"additionalSystemPrompt": "raw dict path"}
    node = McpHookBridgeNode()
    result = node(_state("splunk__submit_report", {"status": "done", "__hook__": hook}))
    assert result == {"pending_hook_output": hook}


def test_bridge_tool_name_agnostic():
    hook = {"additionalSystemPrompt": "from digicert"}
    node = McpHookBridgeNode()
    for tool in ("digicert__renew_cert", "sfmc__send_campaign", "custom__anything"):
        result = node(_state(tool, _wrap({"__hook__": hook})))
        assert result == {"pending_hook_output": hook}, f"failed for {tool}"
