"""Tests for CwdDomainDetectNode — cwd→domain mapping and the unmapped-cwd reminder."""
from __future__ import annotations

from unittest.mock import patch

from langchain_learning.nodes.cwd_domain_detect import CwdDomainDetectNode


def _state(cwd: str = "", domains: list[str] | None = None, reminder_sent: bool = False) -> dict:
    return {
        "cwd": cwd,
        "domains": domains or [],
        "cwd_domain_reminder_sent": reminder_sent,
        "session_id": "test",
    }


# config.cwd_domain_map is a computed property on a frozen pydantic model — can't
# setattr it directly, so patch the underlying loader it delegates to instead.
_PATCH_TARGET = "src.config._load_cwd_domain_map"


def test_matched_cwd_sets_domain_no_reminder():
    with patch(_PATCH_TARGET, return_value={"claude-hooks": "claude-hooks"}):
        node = CwdDomainDetectNode()
        result = node(_state(cwd="/Users/x/workspace/claude-hooks-dev"))

    assert result["domains"] == ["claude-hooks"]
    assert result["cwd_unmapped"] is False
    assert result["cwd_domain_reminder_sent"] is False


def test_unmapped_cwd_first_turn_flags_reminder():
    with patch(_PATCH_TARGET, return_value={"claude-hooks": "claude-hooks"}):
        node = CwdDomainDetectNode()
        result = node(_state(cwd="/Users/x/workspace/some-new-repo"))

    assert result["domains"] == []
    assert result["cwd_unmapped"] is True
    assert result["cwd_domain_reminder_sent"] is True


def test_unmapped_cwd_already_reminded_this_session_stays_quiet():
    with patch(_PATCH_TARGET, return_value={"claude-hooks": "claude-hooks"}):
        node = CwdDomainDetectNode()
        result = node(_state(cwd="/Users/x/workspace/some-new-repo", reminder_sent=True))

    assert result["cwd_unmapped"] is False
    assert result["cwd_domain_reminder_sent"] is True


def test_empty_cwd_never_flags_reminder():
    with patch(_PATCH_TARGET, return_value={"claude-hooks": "claude-hooks"}):
        node = CwdDomainDetectNode()
        result = node(_state(cwd=""))

    assert result["cwd_unmapped"] is False
