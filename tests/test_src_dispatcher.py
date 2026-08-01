"""Tests for src/dispatcher.py — FastMCP domain dispatcher."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.dispatcher import _wrap, _load_module, DOMAIN_MAP, build_dispatcher


# ── _wrap ─────────────────────────────────────────────────────────────────────

def test_wrap_sync_handler_is_callable():
    def handler(x: int) -> int:
        return x * 2
    wrapped = _wrap("domain", handler)
    assert wrapped(x=3) == 6


def test_wrap_preserves_name_and_doc():
    def handler():
        """My docstring."""
    wrapped = _wrap("domain", handler)
    assert wrapped.__name__ == "handler"
    assert wrapped.__doc__ == "My docstring."


def test_wrap_async_handler():
    import asyncio

    async def handler(x: int) -> int:
        return x + 1

    wrapped = _wrap("domain", handler)
    import inspect
    assert inspect.iscoroutinefunction(wrapped)
    result = asyncio.get_event_loop().run_until_complete(wrapped(x=5))
    assert result == 6


# ── _load_module ──────────────────────────────────────────────────────────────

def test_load_module_loads_tools_memory():
    mod = _load_module("tools.memory")
    assert hasattr(mod, "handle_add")


def test_load_module_loads_tools_memory():
    """Replaces the tools.tasks version of this test — that module is gone
    (task:87ec7876), along with its "tasks" entry in DOMAIN_MAP below."""
    mod = _load_module("tools.memory")
    assert hasattr(mod, "handle_add")


# ── DOMAIN_MAP ────────────────────────────────────────────────────────────────

def test_domain_map_has_required_domains():
    assert "memory" in DOMAIN_MAP
    assert "hooks" in DOMAIN_MAP
    assert "code_rag" in DOMAIN_MAP


def test_domain_map_has_no_tasks_entry():
    """tasks__* tools have no implementation left to dispatch to (task:87ec7876)."""
    assert "tasks" not in DOMAIN_MAP


# ── build_dispatcher ──────────────────────────────────────────────────────────

def test_build_dispatcher_registers_tools():
    mcp = MagicMock()
    mcp.tool.return_value = lambda fn: fn  # decorator passthrough
    build_dispatcher(mcp)
    # Should have called mcp.tool once per action across all domains
    total_actions = sum(len(actions) for _, (_, actions) in DOMAIN_MAP.items())
    assert mcp.tool.call_count == total_actions
