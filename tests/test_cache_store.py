"""Tests for hooks/cache_store.py — the persist=True disk-backed cache path.

task:48fdf204 — the vault_context cache must survive a hook-server restart so
the EDEADLK fallback in dispatcher._load_vault_context still has content when
dev_personality.md (iCloud-dataless) can't be read on a cold server.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import cache_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Fresh registry + a tmp cache dir per test."""
    monkeypatch.setattr(cache_store, "_caches", {})
    monkeypatch.setattr(cache_store, "_cache_path",
                        lambda name: tmp_path / ".cache" / f"{name}.json")
    return tmp_path


def test_persisted_cache_round_trips_across_a_fresh_get_cache():
    cache_store.get_cache("vault_context", persist=True)["dev_personality"] = "terse"

    # simulate a server restart: registry cleared, disk file kept
    cache_store._caches.clear()

    reloaded = cache_store.get_cache("vault_context", persist=True)
    assert reloaded["dev_personality"] == "terse"


def test_write_through_file_is_valid_json(_isolated_registry):
    cache_store.get_cache("vault_context", persist=True)["k"] = "v"
    path = _isolated_registry / ".cache" / "vault_context.json"
    assert path.exists()
    import json
    assert json.loads(path.read_text()) == {"k": "v"}


def test_corrupt_file_starts_empty_without_raising(_isolated_registry):
    path = _isolated_registry / ".cache" / "vault_context.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json at all")

    cache = cache_store.get_cache("vault_context", persist=True)
    assert cache == {}
    # and it recovers — a subsequent write overwrites the bad file
    cache["dev_personality"] = "ok now"
    import json
    assert json.loads(path.read_text()) == {"dev_personality": "ok now"}


def test_write_failure_is_swallowed(_isolated_registry, monkeypatch):
    cache = cache_store.get_cache("vault_context", persist=True)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    cache["dev_personality"] = "still works in memory"  # must not raise
    assert cache["dev_personality"] == "still works in memory"


def test_non_persist_cache_writes_no_file(_isolated_registry):
    cache_store.get_cache("ephemeral")["x"] = "y"
    assert not (_isolated_registry / ".cache" / "ephemeral.json").exists()


def test_first_get_cache_wins(_isolated_registry):
    persisted = cache_store.get_cache("vault_context", persist=True)
    again = cache_store.get_cache("vault_context")  # persist defaults False
    assert again is persisted


def test_list_caches_includes_persisted(_isolated_registry):
    cache_store.get_cache("vault_context", persist=True)["dev_personality"] = "t"
    assert cache_store.list_caches()["vault_context"] == ["dev_personality"]
