"""Read the Obsidian vault map: which dir / note holds what, built from the vault SQLite index.

The graph is a cache over the vault: nodes are dirs, notes and tags; edges are
`contains`, `links_to` and `tagged`. It is derived from ~/.claude/vault_index.sqlite
(src/tools/vault_index.py), which the PostToolUse hook keeps current after vault
writes and `python -m src.tools.vault_index` refreshes in full -- never edited by
hand. These tools only read it. The in-memory graph is rebuilt whenever the
database file changes, so an index update needs no server restart.

Set VAULT_INDEX_DB to read an index elsewhere. scripts/vault_map.py still writes
VAULT_MAP.json / VAULT_MAP.md in the vault as a standalone export; these tools no
longer read them.
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

from src.tools.vault_index import connect, reindex_if_stale, words as _words

_DEFAULT_PATH = Path.home() / ".claude/vault_index.sqlite"
_MAX_ROWS = 50

# A dated run file (2026-10-16.md, 10th-Aug-2020.md, NIFTY_WEEKLY_2026-W14.md, X_2026-05-15.md) is one
# instalment of a series; the map describes the series once per folder, not each file.
_DATED = re.compile(r"(?<!\d)(?:(?:19|20)\d\d-(?:W\d\d|\d\d(?:-\d\d)?)|\d{1,2}(?:st|nd|rd|th)-[A-Za-z]{3}-(?:19|20)\d\d)(?!\d)")

_cache: dict = {"path": None, "sig": None, "nodes": {}, "out": {}, "inc": {}}


def _path() -> Path:
    return Path(os.environ.get("VAULT_INDEX_DB") or _DEFAULT_PATH)


def _stamp(stem: str) -> str:
    return _DATED.search(stem).group(0)


def _iso(stamp: str) -> str:
    """Sortable form of a date stamp (`10th-Aug-2020` -> `2020-08-10`); ISO and week stamps pass through."""
    m = re.match(r"(\d{1,2})\w\w-([A-Za-z]{3})-(\d{4})", stamp)
    if not m:
        return stamp
    mon = "janfebmaraprmayjunjulaugsepoctnovdec".find(m.group(2).lower()) // 3 + 1
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"


def _series_key(path: str) -> str | None:
    """Filename with its date stamp replaced by `*`, or None if the note is undated."""
    stem = Path(path).stem
    return _DATED.sub("*", stem, count=1) if _DATED.search(stem) else None


def _build(p: Path) -> tuple[dict, dict, dict]:
    """Derive the dir / note / tag graph from the index tables."""
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        files = conn.execute("SELECT path, title, bytes FROM files ORDER BY path").fetchall()
        heads: dict[str, list[str]] = {}
        for path, heading in conn.execute("SELECT path, heading FROM sections WHERE level >= 1 ORDER BY path, line_start"):
            heads.setdefault(path, []).append(heading)
        tags: dict[str, set[str]] = {}
        for path, tag in conn.execute("SELECT path, tag FROM tags"):
            tags.setdefault(path, set()).add(tag)
        links = conn.execute("SELECT src_path, dst_path FROM links_resolved").fetchall()
    finally:
        conn.close()

    nodes: dict[str, dict] = {}
    out: dict[str, list] = {}
    inc: dict[str, list] = {}

    def edge(a: str, rel: str, b: str) -> None:
        out.setdefault(a, []).append((rel, b))
        inc.setdefault(b, []).append((rel, a))

    dirs: dict[str, dict] = {}
    series: dict[str, dict[str, list[str]]] = {}  # dir -> series pattern -> dated stems
    for path, title, nbytes in files:
        hs = heads.get(path, [])
        ftags = sorted(tags.get(path, ()))
        nodes[path] = {"id": path, "type": "file", "title": title or Path(path).stem,
                       "headings": hs[1:6], "tags": ftags[:10], "bytes": nbytes or 0}
        parts = Path(path).parts[:-1]
        for i in range(len(parts) + 1):
            d = "/".join(parts[:i]) or "."
            e = dirs.setdefault(d, {"files": 0, "tags": Counter(), "words": Counter(),
                                    "samples": [], "children": set()})
            e["files"] += 1
            key = _series_key(path)
            if key:
                if i == len(parts):
                    series.setdefault(d, {}).setdefault(key, []).append(Path(path).stem)
                continue  # instalments don't drive a folder's keywords, tags or samples
            e["tags"].update(ftags)
            e["words"].update(_words(" ".join(hs[:6]) or (title or "")))
            if i == len(parts) and len(e["samples"]) < 5:
                e["samples"].append(Path(path).name)
            if i < len(parts):
                e["children"].add("/".join(parts[:i + 1]))
    for d in sorted(dirs):
        e = dirs[d]
        nodes[d] = {"id": d, "type": "dir", "files": e["files"],
                    "top_tags": [t for t, _ in e["tags"].most_common(6)],
                    "top_words": [w for w, _ in e["words"].most_common(8)], "samples": e["samples"],
                    "series": [{"pattern": k, "count": len(v), "first": min(map(_stamp, v), key=_iso),
                                "last": max(map(_stamp, v), key=_iso)}
                               for k, v in sorted(series.get(d, {}).items())]}
        for c in sorted(e["children"]):
            edge(d, "contains", c)
    for path, _, _ in files:
        edge("/".join(Path(path).parts[:-1]) or ".", "contains", path)
    for src, dst in links:
        if src in nodes and dst in nodes:
            edge(src, "links_to", dst)
    by_tag: dict[str, list[str]] = {}
    for path, ts in tags.items():
        for t in ts:
            if path in nodes:
                by_tag.setdefault(t, []).append(path)
    for t, ids in sorted(by_tag.items()):
        nodes[f"tag:{t}"] = {"id": f"tag:{t}", "type": "tag", "files": len(ids)}
        for i in sorted(ids):
            edge(i, "tagged", f"tag:{t}")
    return nodes, out, inc


def _load() -> dict | None:
    """Return the cached graph, or None if the index is missing or unreadable.

    The index is brought current first (reindex_if_stale, at most once a minute), so
    notes edited outside the MCP -- in Obsidian -- show up without anyone running a build.
    """
    p = _path()
    try:
        conn = connect(p)
        try:
            reindex_if_stale(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass  # fall through: serve what is there, or report it missing
    try:
        st = p.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    if _cache["path"] == p and _cache["sig"] == sig:
        return _cache
    try:
        nodes, out, inc = _build(p)
    except sqlite3.Error:
        return None
    _cache.update(path=p, sig=sig, nodes=nodes, out=out, inc=inc)
    return _cache


def _index_state() -> dict:
    """When the index last did a full pass and the last hook failure, so staleness is visible."""
    try:
        conn = sqlite3.connect(f"file:{_path()}?mode=ro", uri=True)
        try:
            m = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('last_full_build', 'last_error')"))
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {"last_full_build": m.get("last_full_build"), "last_error": m.get("last_error") or None}


def _missing() -> dict:
    return {"error": f"vault index not found at {_path()}; run `uv run python -m src.tools.vault_index` in claude-hooks"}


def _brief(n: dict) -> dict:
    if n["type"] == "dir":
        return {"id": n["id"], "type": "dir", "files": n["files"],
                "top_words": n["top_words"], "top_tags": n["top_tags"], "series": n["series"]}
    if n["type"] == "tag":
        return {"id": n["id"], "type": "tag", "files": n["files"]}
    return {"id": n["id"], "type": "file", "title": n["title"], "tags": n["tags"]}


def handle_overview(dir: str = ".", depth: int = 1) -> dict:
    """What lives where: the subdirectories of `dir` (vault-relative, "." for the
    root) down `depth` levels, each with note count, top keywords and top tags."""
    g = _load()
    if g is None:
        return _missing()
    if dir not in g["nodes"] or g["nodes"][dir]["type"] != "dir":
        return {"error": f"no directory {dir!r} in the vault map"}
    rows, frontier = [], [dir]
    for _ in range(max(depth, 1)):
        nxt = []
        for d in frontier:
            for rel, to in g["out"].get(d, []):
                if rel == "contains" and g["nodes"][to]["type"] == "dir":
                    rows.append(_brief(g["nodes"][to]))
                    nxt.append(to)
        frontier = nxt
    rows.sort(key=lambda r: r["id"])
    files = [g["nodes"][to]["title"] and {"id": to, "title": g["nodes"][to]["title"]}
             for rel, to in g["out"].get(dir, [])
             if rel == "contains" and g["nodes"][to]["type"] == "file" and not _series_key(to)]
    return {"dir": _brief(g["nodes"][dir]), "index": _index_state(), "subdirs": rows[:_MAX_ROWS],
            "files": files[:_MAX_ROWS], "truncated": len(rows) > _MAX_ROWS or len(files) > _MAX_ROWS}


def handle_node(id: str) -> dict:
    """One node (dir, note path, or `tag:<name>`) with its edges grouped by
    relation and direction: contains, links_to, tagged."""
    g = _load()
    if g is None:
        return _missing()
    n = g["nodes"].get(id)
    if n is None:
        return {"error": f"no node {id!r}"}
    def group(pairs):
        d: dict[str, list] = {}
        for rel, other in pairs:
            d.setdefault(rel, []).append(other)
        return {k: v[:_MAX_ROWS] for k, v in d.items()}
    return {"node": n, "out": group(g["out"].get(id, [])), "in": group(g["inc"].get(id, []))}


def handle_tag(tag: str) -> dict:
    """Notes carrying a tag (with or without the leading #), plus how many sit
    in each top-level folder -- the cross-directory view."""
    g = _load()
    if g is None:
        return _missing()
    tid = "tag:" + tag.lstrip("#")
    if tid not in g["nodes"]:
        return {"error": f"no tag {tag!r}"}
    ids = [f for rel, f in g["inc"].get(tid, []) if rel == "tagged"]
    by_dir: dict[str, int] = {}
    for i in ids:
        top = i.split("/")[0]
        by_dir[top] = by_dir.get(top, 0) + 1
    return {"tag": tid, "count": len(ids), "by_top_dir": by_dir,
            "files": [{"id": i, "title": g["nodes"][i]["title"]} for i in ids[:_MAX_ROWS]]}


def handle_find(query: str, limit: int = 20) -> dict:
    """Case-insensitive substring match over note titles, headings, tags and
    paths, and over directory keywords. Cheap pointer to *where* to look; use
    vault_rag__fts_vault for note bodies."""
    g = _load()
    if g is None:
        return _missing()
    q = query.lower()
    hits = []
    for n in g["nodes"].values():
        if n["type"] == "tag":
            hay = n["id"]
        elif n["type"] == "dir":
            hay = " ".join([n["id"], *n["top_words"], *n["top_tags"]])
        else:
            hay = " ".join([n["id"], n["title"], *n["headings"], *n["tags"]])
        if q in hay.lower():
            hits.append(_brief(n))
    hits.sort(key=lambda h: (h["type"] != "dir", h["id"]))
    return {"query": query, "count": len(hits), "hits": hits[:min(limit, _MAX_ROWS)]}
