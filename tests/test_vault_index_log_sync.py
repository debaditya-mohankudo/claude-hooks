"""vault_index follows the vault file log instead of walking the vault again (task:371976dd).

The log (cli-graph's mac/vault_file_log.py, behind `mac vault_log`) is the one walk;
sync_from_log re-parses only the notes its events name. The end-to-end tests run the real
log script against a scratch vault and skip when cli-graph is not checked out.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import src.tools.vault_index as vi

LOG_SCRIPT = Path.home() / "workspace/cli-graph/mac/vault_file_log.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Daily").mkdir(parents=True)
    (root / "Daily/a.md").write_text("# Alpha\n\nfirst\n")
    (root / "Daily/b.md").write_text("# Beta\n")
    log = tmp_path / "log.sqlite"
    monkeypatch.setenv("VAULT_ROOT", str(root))
    monkeypatch.setenv("VAULT_FILE_LOG_DB", str(log))
    monkeypatch.setenv("VAULT_INDEX_DB", str(tmp_path / "idx.sqlite"))
    return root, log


def scan(root, log):
    if not LOG_SCRIPT.is_file():
        pytest.skip(f"vault_file_log script not found: {LOG_SCRIPT}")
    out = subprocess.run([sys.executable, "-I", str(LOG_SCRIPT), "--json", "scan"], capture_output=True, text=True,
                         env={**os.environ, "VAULT": str(root), "VAULT_FILE_LOG_DB": str(log)}, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def paths(conn):
    return {r[0] for r in conn.execute("SELECT path FROM files")}


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_no_log_means_no_sync(tmp_path):
    conn = vi.connect(tmp_path / "idx.sqlite")
    assert vi.sync_from_log(conn, tmp_path, tmp_path / "missing.sqlite") is None


def test_first_sync_rebuilds_then_follows_the_log_only(env, monkeypatch):
    root, log = env
    scan(root, log)  # baseline: versions, no events
    conn = vi.connect()
    first = vi.sync_from_log(conn)
    assert first["mode"] == "rebuild" and paths(conn) == {"Daily/a.md", "Daily/b.md"}

    (root / "Daily/a.md").write_text("# Alpha\n\nchanged with zebra\n")
    (root / "Daily/b.md").unlink()
    (root / "Daily/c.md").write_text("# Gamma\n")
    (root / "Daily/notes.txt").write_text("not a note")
    os.utime(root / "Daily/a.md", (1, 1))  # a different mtime even on a coarse clock
    res = scan(root, log)
    assert (res["created"], res["edited"], res["deleted"]) == (2, 1, 1)

    monkeypatch.setattr(vi, "_walk", lambda r: pytest.fail("sync_from_log walked the vault"))
    second = vi.sync_from_log(conn)
    assert second["mode"] == "log"
    assert (second["indexed"], second["removed"]) == (2, 1)  # notes.txt is not indexable
    assert paths(conn) == {"Daily/a.md", "Daily/c.md"}
    assert [h["path"] for h in vi.search(conn, "zebra")] == ["Daily/a.md"]
    assert vi.sync_from_log(conn) == {"mode": "log", "indexed": 0, "removed": 0, "unreadable": 0,
                                      "log_event_id": second["log_event_id"]}


def test_sync_never_writes_the_log_and_a_schema_bump_leaves_it_untouched(env, monkeypatch):
    root, log = env
    scan(root, log)
    (root / "Daily/c.md").write_text("# Gamma\n")
    scan(root, log)
    before = digest(log)
    conn = vi.connect()
    vi.sync_from_log(conn)
    conn.close()
    monkeypatch.setattr(vi, "SCHEMA_VERSION", "999")  # connect() drops every index table
    conn = vi.connect()
    assert paths(conn) == set()
    vi.sync_from_log(conn)
    assert digest(log) == before
    assert paths(conn) == {"Daily/a.md", "Daily/b.md", "Daily/c.md"}  # dropped mark -> rebuild


def test_a_mark_past_the_log_rebuilds(env):
    root, log = env
    scan(root, log)
    conn = vi.connect()
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('log_event_id', '99999')")  # log restored from backup
    assert vi.sync_from_log(conn)["mode"] == "rebuild"


def test_handle_sync_from_log_fails_open(env, monkeypatch):
    root, log = env
    scan(root, log)
    monkeypatch.setattr(vi, "reindex_all", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    res = vi.handle_sync_from_log()
    assert res == {"error": "vault index sync failed: RuntimeError: disk"}
    conn = vi.connect()
    assert "sync_from_log: disk" in conn.execute("SELECT value FROM meta WHERE key='last_error'").fetchone()[0]


def test_handle_sync_from_log_without_a_log(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_FILE_LOG_DB", str(tmp_path / "none.sqlite"))
    monkeypatch.setenv("VAULT_INDEX_DB", str(tmp_path / "idx.sqlite"))
    assert "skipped" in vi.handle_sync_from_log()


def test_reindex_if_stale_scans_instead_of_walking_on_the_real_vault(env, monkeypatch):
    root, log = env
    monkeypatch.setattr(vi, "VAULT", root)  # the log covers this root
    scan(root, log)
    conn = vi.connect()
    vi.sync_from_log(conn)
    (root / "Daily/c.md").write_text("# Gamma\n")
    monkeypatch.setattr(vi, "_run_log_scan", lambda: bool(scan(root, log)))
    monkeypatch.setattr(vi, "reindex_all", lambda *a, **k: pytest.fail("walked the vault"))
    assert vi.reindex_if_stale(conn, ttl_s=0) is True
    assert "Daily/c.md" in paths(conn)


def test_reindex_if_stale_walks_when_the_scan_fails(env, monkeypatch):
    root, log = env
    monkeypatch.setattr(vi, "VAULT", root)
    scan(root, log)
    conn = vi.connect()
    monkeypatch.setattr(vi, "_run_log_scan", lambda: False)
    assert vi.reindex_if_stale(conn, ttl_s=0) is True
    assert paths(conn) == {"Daily/a.md", "Daily/b.md"}


def test_reindex_if_stale_walks_a_root_the_log_does_not_cover(env, monkeypatch):
    conn = vi.connect()
    monkeypatch.setattr(vi, "_run_log_scan", lambda: pytest.fail("scanned the real vault for a test root"))
    assert vi.reindex_if_stale(conn, ttl_s=0) is True
    assert paths(conn) == {"Daily/a.md", "Daily/b.md"}
