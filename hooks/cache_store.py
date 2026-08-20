"""In-memory cache registry, shared by the long-running hook server process.

Holds named caches (e.g. "vault_context") that survive across hook calls
within the server process — dispatcher.py is imported once into
hooks/server.py's process, not re-executed per call — but are discarded on
restart, same as every other bit of in-process state this server holds
(hooks/session_state.py). Callers own their own key naming within a cache;
this module is just shared storage plus a read API for hooks/server.py.
"""

_caches: dict[str, dict[str, str]] = {}


def get_cache(name: str) -> dict[str, str]:
    """Return the named cache dict, creating it empty on first use."""
    return _caches.setdefault(name, {})


def list_caches() -> dict[str, list[str]]:
    """Cache name -> its current keys, for a cheap overview without dumping content."""
    return {name: list(cache.keys()) for name, cache in _caches.items()}
