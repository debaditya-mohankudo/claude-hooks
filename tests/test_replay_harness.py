"""Pytest wrapper for the replay harness.

Loads the saved baseline and replays it against the current graph.
Skips if the hooks DB (iCloud) is not available — CI-safe.
"""
import json
import pytest
from pathlib import Path

from tests.replay_harness import HOOKS_DB, BASELINE, cmd_replay
import argparse


@pytest.fixture(scope="module")
def _replay_args():
    args = argparse.Namespace(since="2026-06-13", limit=100)
    return args


@pytest.mark.skipif(not HOOKS_DB.exists(), reason="claude_hooks.sqlite not available (iCloud offline)")
def test_replay_matches_baseline(_replay_args):
    """Replay baseline UPS events through the current graph — assert 0 deviations."""
    if not BASELINE.exists():
        pytest.skip("replay_baseline.json not found — run replay_harness.py --capture first")

    baseline = json.loads(BASELINE.read_text())
    if not baseline:
        pytest.skip("baseline is empty")

    deviations = cmd_replay(baseline, _replay_args)
    assert deviations == [], (
        f"{len(deviations)} replay deviation(s):\n" +
        "\n".join(
            f"  session={d['session'][:8]} cwd={d['cwd']}\n" +
            "\n".join(f"    {k}: {v['baseline']} → {v['replayed']}" for k, v in d["diffs"].items())
            for d in deviations
        )
    )

