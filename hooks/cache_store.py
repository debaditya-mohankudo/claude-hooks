"""In-memory cache registry, shared by the long-running hook server process.

Holds named caches (e.g. "vault_context") that survive across hook calls
within the server process — dispatcher.py is imported once into
hooks/server.py's process, not re-executed per call. In-process caches are
discarded on restart, same as every other bit of in-process state this
server holds (hooks/session_state.py).

A cache created with persist=True additionally write-throughs to
config.claude_db_dir/.cache/<name>.json and reloads from it on the first
get_cache() after a restart — for content whose upstream source can be
transiently unreadable (task:48fdf204: dev_personality.md is stored
iCloud-dataless, so the server's first read after any restart EDEADLKs and
an in-process-only fallback never gets primed). Persistence fails open in
both directions: a corrupt file starts empty, a failed write is swallowed.

Callers own their own key naming within a cache; this module is just shared
storage plus a read API for hooks/server.py.
"""
from __future__ import annotations

import json

from src.config import config as _cfg
from src.logger import get_logger

_log = get_logger(__name__)

_caches: dict[str, dict[str, str]] = {}


def _cache_path(name: str):
    return _cfg.claude_db_dir / ".cache" / f"{name}.json"


class _PersistentCache(dict):
    """dict that mirrors itself to a JSON file on every mutation.

    Values are small (vault_context holds one ~1KB key), so rewriting the
    whole file per mutation is cheaper than tracking deltas. Every disk
    touch is guarded — the hook path must never break because a cache write
    or read failed.
    """

    def __init__(self, path):
        super().__init__()
        self._path = path
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    super().update({str(k): str(v) for k, v in data.items()})
        except Exception as exc:  # corrupt / unreadable — start empty
            _log.warning("cache_store: could not load %s: %s", path, exc)

    def _flush(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(dict(self), ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            _log.warning("cache_store: could not write %s: %s", self._path, exc)

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self._flush()

    def __delitem__(self, key) -> None:
        super().__delitem__(key)
        self._flush()

    def update(self, *args, **kwargs) -> None:  # type: ignore[override]
        super().update(*args, **kwargs)
        self._flush()

    def pop(self, *args, **kwargs):  # type: ignore[override]
        result = super().pop(*args, **kwargs)
        self._flush()
        return result

    def setdefault(self, key, default=None):  # type: ignore[override]
        missing = key not in self
        result = super().setdefault(key, default)
        if missing:
            self._flush()
        return result


def get_cache(name: str, *, persist: bool = False) -> dict[str, str]:
    """Return the named cache dict, creating it on first use.

    persist=True backs the cache with config.claude_db_dir/cache/<name>.json
    (loaded now, written through on mutation). Only honoured when the cache
    is first created — a later get_cache(name) returns whatever was made the
    first time, matching the previous setdefault() semantics.
    """
    cache = _caches.get(name)
    if cache is None:
        cache = _PersistentCache(_cache_path(name)) if persist else {}
        _caches[name] = cache
    return cache


def list_caches() -> dict[str, list[str]]:
    """Cache name -> its current keys, for a cheap overview without dumping content."""
    return {name: list(cache.keys()) for name, cache in _caches.items()}
