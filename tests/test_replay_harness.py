"""Replay harness: its own behaviour on a temp store, then the local baseline if one was captured."""
import json
from datetime import datetime, timezone

import pytest

from tests import replay_harness as rh
from tests.fixtures.db_factories import make_memory_db


def test_replay_returns_slugs_from_the_snapshot_and_writes_nothing(tmp_path, monkeypatch):
    row = {"name": "zebra-quartz", "type": "project", "tags": "zebra quartz"}
    live, snap = make_memory_db(tmp_path, [row]), tmp_path / "snap.sqlite"
    rh.snapshot_memory_db(snap, source=live)
    make_memory_db(tmp_path, [{**row, "name": "zebra-quartz-later"}])  # added after capture: must not show

    writes = []
    monkeypatch.setattr(rh._memory_scoring, "record_memory_hits", writes.append)
    monkeypatch.setattr(rh._memory_audit, "record", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(rh.hooks.server_memory, "record_memories", lambda *a: writes.append(a))

    prompts = [{"id": 1, "session": "s", "prompt": "tell me about zebra quartz please"}]
    out = rh.replay(prompts, snap, datetime.now(timezone.utc))

    assert out[0]["memories"] == ["zebra-quartz"]
    assert writes == []


def test_diff_catches_a_different_slug_with_the_same_count():
    base = [{"id": 1, "prompt": "p", "memories": ["a"]}]
    assert rh.diff_events(base, [{**base[0], "memories": ["b"]}])
    assert rh.diff_events(base, base) == []


def test_replay_matches_baseline():
    """Needs a local capture (gitignored): replay_harness.py --capture."""
    if not rh.BASELINE.exists():
        pytest.skip("replay_baseline.json not found — run replay_harness.py --capture first")
    deviations = rh.cmd_replay(json.loads(rh.BASELINE.read_text()))
    assert deviations == [], "\n".join(rh.format_deviation(d) for d in deviations)
