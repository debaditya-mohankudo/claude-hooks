"""Tests for CwdDomainDetectNode — cwd→domain mapping."""
from __future__ import annotations

from unittest.mock import patch

from langchain_learning.nodes.cwd_domain_detect import CwdDomainDetectNode


def _state(cwd: str = "", domains: list[str] | None = None) -> dict:
    return {
        "cwd": cwd,
        "domains": domains or [],
        "session_id": "test",
    }


# config.cwd_domain_map is a computed property on a frozen pydantic model — can't
# setattr it directly, so patch the underlying loader it delegates to instead.
_PATCH_TARGET = "src.config._load_cwd_domain_map"


def test_matched_cwd_sets_domain():
    with patch(_PATCH_TARGET, return_value={"claude-hooks": "claude-hooks"}):
        node = CwdDomainDetectNode()
        result = node(_state(cwd="/Users/x/workspace/claude-hooks-dev"))

    assert result["domains"] == ["claude-hooks"]


def test_unmapped_cwd_leaves_domains_unchanged():
    with patch(_PATCH_TARGET, return_value={"claude-hooks": "claude-hooks"}):
        node = CwdDomainDetectNode()
        result = node(_state(cwd="/Users/x/workspace/some-new-repo"))

    assert result["domains"] == []


def test_empty_cwd_leaves_domains_unchanged():
    with patch(_PATCH_TARGET, return_value={"claude-hooks": "claude-hooks"}):
        node = CwdDomainDetectNode()
        result = node(_state(cwd=""))

    assert result["domains"] == []
