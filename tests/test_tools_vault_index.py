"""Tests for src/tools/vault_index.py -- derived SQLite index over the vault."""
import os

import pytest

import src.tools.vault_index as vi


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Market/Macro").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / ".obsidian/x.md").write_text("# hidden")
    (root / "Market/Macro/US Macro.md").write_text("# US Macro\n\nRun logs: [[US Macro run logs archive]]\n")
    (root / "Market/Macro/US Macro run logs archive.md").write_text(
        "---\ntags: [macro-run-log, us-macro]\ngraph: ontology/x.json\n---\n"
        "# US Macro run logs archive\n\nIntro with #inline-tag and [[US Macro]].\n\n"
        "## Fed rate-hike odds run 2026-10-17 (watch_next row 188)\n\nOct hike 15% after Warsh.\n"
        "```\n# not a heading\n```\n\n## Older run 2026-10-16\n\nHold.\n")
    return root


@pytest.fixture
def conn(tmp_path):
    return vi.connect(tmp_path / "idx.sqlite")


def test_parse_frontmatter_sections_and_dates(vault):
    note = vi.parse_note((vault / "Market/Macro/US Macro run logs archive.md").read_text())
    assert note["tags"] == ["inline-tag", "macro-run-log", "us-macro"]
    assert note["props"] == {"graph": "ontology/x.json"}
    heads = [(s["level"], s["heading"], s["entry_date"]) for s in note["sections"]]
    assert (2, "Fed rate-hike odds run 2026-10-17 (watch_next row 188)", "2026-10-17") in heads
    assert not any("not a heading" in s["heading"] for s in note["sections"])
    fed = next(s for s in note["sections"] if s["entry_date"] == "2026-10-17")
    assert fed["line_start"] < fed["line_end"] and "Warsh" in fed["body"]


def test_reindex_all_tags_links_and_skips_hidden(vault, conn):
    stats = vi.reindex_all(conn, vault)
    assert stats["indexed"] == 2
    tagged = {r[0] for r in conn.execute("SELECT path FROM tags WHERE tag='macro-run-log'")}
    assert tagged == {"Market/Macro/US Macro run logs archive.md"}
    assert conn.execute("SELECT count(*) FROM files WHERE path LIKE '.obsidian%'").fetchone()[0] == 0
    links = set(conn.execute("SELECT src_path, dst_path FROM links_resolved"))
    assert ("Market/Macro/US Macro.md", "Market/Macro/US Macro run logs archive.md") in links


def test_incremental_pick_up_change_and_delete(vault, conn):
    vi.reindex_all(conn, vault)
    assert vi.reindex_all(conn, vault) == {"indexed": 0, "unchanged": 2, "removed": 0, "unreadable": 0}
    note = vault / "Market/Macro/US Macro run logs archive.md"
    note.write_text(note.read_text() + "\n## Newer run 2026-10-18\n\nCME hike odds moved.\n")
    os.utime(note, (note.stat().st_atime, note.stat().st_mtime + 5))
    assert vi.reindex_all(conn, vault)["indexed"] == 1
    assert vi.search(conn, "moved")[0]["entry_date"] == "2026-10-18"
    (vault / "Market/Macro/US Macro.md").unlink()
    assert vi.reindex_all(conn, vault)["removed"] == 1
    assert conn.execute("SELECT count(*) FROM links WHERE src_path LIKE '%/US Macro.md'").fetchone()[0] == 0


def test_search_returns_section_not_just_file(vault, conn):
    vi.reindex_all(conn, vault)
    hit = vi.search(conn, "Warsh")[0]
    assert hit["heading"].startswith("Fed rate-hike odds run 2026-10-17")
    assert hit["entry_date"] == "2026-10-17"


def test_reindex_for_tool_updates_index_and_ignores_other_tools(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_ROOT", str(vault))
    monkeypatch.setenv("VAULT_INDEX_DB", str(tmp_path / "hook.sqlite"))
    note = "Market/Macro/US Macro.md"
    assert vi.reindex_for_tool("vault__read", {"path": note}) is None
    assert f"{note}=indexed" in vi.reindex_for_tool("vault__append", {"path": note, "content": "x"})
    conn = vi.connect()
    assert conn.execute("SELECT count(*) FROM files").fetchone() == (1,)
    (vault / note).unlink()
    assert f"{note}=removed" in vi.reindex_for_tool("vault__delete", {"path": note})
    assert conn.execute("SELECT count(*) FROM files").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM fts").fetchone() == (0,)
    assert "no .md path named" in vi.reindex_for_tool("vault__move", {"path": "folder", "to": "other"})


def test_reindex_for_tool_move_reindexes_both_paths(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_ROOT", str(vault))
    monkeypatch.setenv("VAULT_INDEX_DB", str(tmp_path / "hook.sqlite"))
    src, dst = "Market/Macro/US Macro.md", "Market/Macro/Renamed.md"
    vi.reindex_for_tool("vault__write", {"path": src})
    (vault / src).rename(vault / dst)
    res = vi.reindex_for_tool("vault__move", {"path": src, "to": dst})
    assert f"{src}=removed" in res and f"{dst}=indexed" in res


def test_reindex_for_tool_records_failure_in_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("VAULT_INDEX_DB", str(tmp_path / "hook.sqlite"))
    vi.connect().close()
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(vi, "reindex_file", boom)
    assert vi.reindex_for_tool("vault__append", {"path": "n.md"}) is None
    assert "disk on fire" in vi.connect().execute("SELECT value FROM meta WHERE key='last_error'").fetchone()[0]


def test_unreadable_note_gets_a_stub_row(vault, conn):
    p = vault / "Market/Macro/US Macro.md"
    p.chmod(0)
    try:
        if os.access(p, os.R_OK):
            pytest.skip("running as a user who can read mode-000 files")
        assert vi.reindex_all(conn, vault)["unreadable"] == 1
    finally:
        p.chmod(0o644)
    assert vi.reindex_all(conn, vault)["indexed"] >= 1  # retried and parsed once readable


def test_reindex_if_stale_respects_ttl(vault, conn):
    assert vi.reindex_if_stale(conn, ttl_s=60) is True  # never built
    assert vi.reindex_if_stale(conn, ttl_s=60) is False  # just built
    assert vi.reindex_if_stale(conn, ttl_s=0) is True


def test_old_schema_version_is_rebuilt(vault, tmp_path):
    db = tmp_path / "old.sqlite"
    conn = vi.connect(db)
    vi.reindex_all(conn, vault)
    conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    conn = vi.connect(db)
    assert conn.execute("SELECT count(*) FROM files").fetchone() == (0,)
    assert vi.reindex_all(conn, vault)["indexed"] == 2
    assert vi.search(conn, "Warsh")
