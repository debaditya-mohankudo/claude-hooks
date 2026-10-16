"""Tests for src/tools/vault_map.py -- read-only tools over the vault SQLite index."""
import os

import pytest

import src.tools.vault_index as vi
import src.tools.vault_map as vm


@pytest.fixture(autouse=True)
def _index(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Daily").mkdir(parents=True)
    (root / "Stocks").mkdir()
    (root / "Daily/a.md").write_text("---\ntags: [fii]\n---\n# FII flows\n\n## Nifty\n\nsee [[c]]\n")
    (root / "Daily/b.md").write_text("---\ntags: [fii]\n---\n# Gold\n")
    (root / "Stocks/c.md").write_text("# Tracker\n")
    db = tmp_path / "idx.sqlite"
    monkeypatch.setenv("VAULT_INDEX_DB", str(db))
    monkeypatch.setenv("VAULT_ROOT", str(root))
    conn = vi.connect(db)
    vi.reindex_all(conn, root)
    conn.close()
    vm._cache.update(path=None, sig=None)
    return root


def test_overview_lists_subdirs_with_counts():
    r = vm.handle_overview()
    assert [d["id"] for d in r["subdirs"]] == ["Daily", "Stocks"]
    assert r["subdirs"][0]["files"] == 2


def test_overview_unknown_dir_errors():
    assert "error" in vm.handle_overview("Nope")


def test_node_shows_links_both_directions():
    assert vm.handle_node("Daily/a.md")["out"]["links_to"] == ["Stocks/c.md"]
    assert vm.handle_node("Stocks/c.md")["in"]["links_to"] == ["Daily/a.md"]


def test_tag_groups_by_top_dir():
    r = vm.handle_tag("#fii")
    assert r["count"] == 2 and r["by_top_dir"] == {"Daily": 2}


def test_find_matches_titles_and_headings():
    hits = vm.handle_find("nifty")["hits"]
    assert [h["id"] for h in hits if h["type"] == "file"] == ["Daily/a.md"]
    dirs = [h["id"] for h in hits if h["type"] == "dir"]  # dir keywords match too, and dirs sort first
    assert "Daily" in dirs and hits[0]["type"] == "dir"


def test_graph_follows_index_updates_without_restart(_index):
    root = _index
    (root / "Stocks/new.md").write_text("---\ntags: [macro-run-log]\n---\n# Fresh note\n")
    conn = vi.connect()
    vi.reindex_file(conn, root, "Stocks/new.md")
    conn.commit()
    conn.close()
    assert vm.handle_tag("macro-run-log")["count"] == 1
    assert "Stocks/new.md" in [h["id"] for h in vm.handle_find("fresh")["hits"]]


def test_missing_index_is_built_on_first_use(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_INDEX_DB", str(tmp_path / "absent.sqlite"))
    vm._cache.update(path=None, sig=None)
    assert [h["id"] for h in vm.handle_find("tracker")["hits"] if h["type"] == "file"] == ["Stocks/c.md"]


def test_unwritable_index_reports_how_to_build(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")  # a parent that is a file: the index can be neither opened nor built
    monkeypatch.setenv("VAULT_INDEX_DB", str(blocker / "idx.sqlite"))
    vm._cache.update(path=None, sig=None)
    assert "vault_index" in vm.handle_find("x")["error"]


def test_overview_reports_index_state():
    st = vm.handle_overview()["index"]
    assert st["last_full_build"] and st["last_error"] is None
