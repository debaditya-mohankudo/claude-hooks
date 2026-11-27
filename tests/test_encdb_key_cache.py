"""The derived-key cache must only ever save time: it never changes whether a DB opens."""
import apsw
import pytest

from src import encdb

KEY = "ab" * 32


def _encrypted(path, key=KEY):
    c = apsw.Connection(str(path))
    c.pragma("cipher", "sqlcipher")
    c.pragma("hexkey", key)
    c.execute("create table t(x)")
    c.execute("insert into t values (1)")
    c.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    keyfile = tmp_path / "memory.key"
    keyfile.write_text(KEY + "\n")
    monkeypatch.setattr(encdb, "KEY_FILE", keyfile)
    monkeypatch.setattr(encdb, "DERIVED_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(encdb, "_key_cache", {})
    db = tmp_path / "enc.sqlite"
    _encrypted(db)
    return db, keyfile, tmp_path / "cache"


def _read(db, **kw):
    con = encdb.open_db(db, **kw)
    try:
        return con.execute("select x from t").fetchone()[0]
    finally:
        con.close()


def _no_derivation(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("derived again")
    monkeypatch.setattr(encdb.hashlib, "pbkdf2_hmac", boom)


def test_second_open_skips_derivation_and_entry_is_owner_only(env, monkeypatch):
    db, _, cache = env
    assert _read(db) == 1
    (entry,) = cache.iterdir()
    assert entry.stat().st_mode & 0o777 == 0o600
    assert cache.stat().st_mode & 0o777 == 0o700
    _no_derivation(monkeypatch)
    assert _read(db) == 1
    assert _read(db, readonly=True) == 1


@pytest.mark.parametrize("bad", ["00" * 32, "garbage", ""])
def test_bad_entry_is_replaced(env, bad):
    db, _, cache = env
    _read(db)
    (entry,) = cache.iterdir()
    good = entry.read_text()
    entry.write_text(bad)
    assert _read(db) == 1
    assert entry.read_text() == good


def test_entry_readable_by_others_is_rewritten_owner_only(env):
    db, _, cache = env
    _read(db)
    (entry,) = cache.iterdir()
    entry.chmod(0o644)
    assert _read(db) == 1
    assert entry.stat().st_mode & 0o777 == 0o600


def test_unwritable_cache_still_opens(env, monkeypatch, tmp_path):
    db, _, _ = env
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(encdb, "DERIVED_CACHE_DIR", blocker / "cache")
    assert _read(db) == 1


def test_rewritten_db_with_a_new_salt_opens(env):
    db, _, cache = env
    _read(db)
    db.unlink()
    _encrypted(db)
    assert _read(db) == 1
    assert len(list(cache.iterdir())) == 2


@pytest.mark.parametrize("content", [None, b"\x00GITCRYPT\x00xxxx"])
def test_missing_or_locked_key_fails_closed_despite_cache(env, content):
    db, keyfile, _ = env
    _read(db)
    encdb._key_cache.clear()
    keyfile.unlink() if content is None else keyfile.write_bytes(content)
    with pytest.raises(encdb.EncDBError):
        encdb.open_db(db)


def test_wrong_master_key_fails_and_caches_nothing(env):
    db, keyfile, cache = env
    keyfile.write_text("cd" * 32)
    with pytest.raises(encdb.EncDBError):
        encdb.open_db(db)
    assert not cache.exists()


def test_explicit_key_does_not_touch_cache(env):
    db, _, cache = env
    assert _read(db, key=KEY) == 1
    assert not cache.exists()
