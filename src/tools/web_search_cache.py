"""SQLite cache of WebSearch results, keyed by query.

Searches repeat across sessions and graph runs, so each result is stored and a
fresh one is served instead of a new search:

    store(query, tool_input, tool_response)   PostToolUse on WebSearch
    lookup(query, tool_input)                 PreToolUse on WebSearch -> hit or None
    search(text) / stats() / purge(days)      CLI reports

Key: the query lowercased with whitespace collapsed, plus the domain filters
and the search mode, so "standard" and "extended" results never mix.

TTL by query shape (a hit past its TTL is a miss, the row is kept for `search`):

    LIVE_TTL_S   30 min  dated or live asks: "today", "latest", "live", "price",
                         "now", a YYYY-MM-DD / "Nov 19" date, "breaking"
    DOC_TTL_S    7 days  reference lookups: "docs", "documentation", "api",
                         "reference", "how to", "syntax"
    DEFAULT_TTL_S 6 h    everything else

`--ttl SECONDS` on the CLI `put`, or `ttl_s` in store(), overrides the rule.
Every hook entry point fails open: an error is logged and returns None.

    uv run python -m src.tools.web_search_cache stats
    uv run python -m src.tools.web_search_cache get "query text"
    uv run python -m src.tools.web_search_cache search "keyword" [--limit N]
    uv run python -m src.tools.web_search_cache purge --days 30
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

TOOL = "WebSearch"
DEFAULT_TTL_S = 6 * 3600
LIVE_TTL_S = 30 * 60
DOC_TTL_S = 7 * 24 * 3600
TEST_PREFIXES = ("test-", "pytest-", "api-test-")

_LIVE = re.compile(
    r"\b(today|tonight|latest|live|now|breaking|current|price|prices|premarket|pre-market"
    r"|overnight|yesterday|this week|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\b"
    r"|\b20\d\d-\d\d-\d\d\b|\b\d{1,2}/\d{1,2}(/\d{2,4})?\b"
)
_DOC = re.compile(r"\b(docs?|documentation|api|reference|how to|syntax|manual|spec)\b")

DDL = """
CREATE TABLE IF NOT EXISTS web_search_cache (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    query       TEXT NOT NULL,
    mode        TEXT,
    domains     TEXT,
    result      TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    ttl_s       INTEGER NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit    REAL,
    session_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_wsc_fetched ON web_search_cache(fetched_at);
"""


def default_db() -> Path:
    from src.config import config
    return config.claude_db_dir / "web_search_cache.sqlite"


def connect(db: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db or default_db()), timeout=5)
    conn.executescript(DDL)
    return conn


def normalize(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip().lower())


def _domains(tool_input: dict) -> str:
    allowed = ",".join(sorted(tool_input.get("allowed_domains") or []))
    blocked = ",".join(sorted(tool_input.get("blocked_domains") or []))
    return f"+{allowed}|-{blocked}" if allowed or blocked else ""


def make_key(query: str, tool_input: dict | None = None) -> str:
    tool_input = tool_input or {}
    raw = "\x1f".join([normalize(query), str(tool_input.get("mode") or "standard"), _domains(tool_input)])
    return hashlib.sha256(raw.encode()).hexdigest()


def ttl_for(query: str) -> int:
    q = normalize(query)
    if _LIVE.search(q):
        return LIVE_TTL_S
    if _DOC.search(q):
        return DOC_TTL_S
    return DEFAULT_TTL_S


def result_text(tool_response) -> str:
    """Plain text of a WebSearch response from any shape the hook receives it in."""
    resp = tool_response
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        for k in ("content", "raw", "result", "results", "text"):
            if k in resp:
                return result_text(resp[k])
        return json.dumps(resp, ensure_ascii=False)
    if isinstance(resp, list):
        parts = []
        for block in resp:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(parts)
    return "" if resp is None else str(resp)


def store(query: str, tool_input: dict | None, tool_response, session_id: str = "",
          ttl_s: int | None = None, db: Path | str | None = None, now: float | None = None) -> bool:
    """Upsert one result. Returns False for an empty query or an empty result."""
    tool_input = tool_input or {}
    text = result_text(tool_response).strip()
    if not normalize(query) or not text:
        return False
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO web_search_cache (key, query, mode, domains, result, fetched_at, ttl_s, session_id)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET result=excluded.result, fetched_at=excluded.fetched_at,"
            " ttl_s=excluded.ttl_s, session_id=excluded.session_id, hits=0, last_hit=NULL",
            (make_key(query, tool_input), query.strip(), str(tool_input.get("mode") or "standard"),
             _domains(tool_input), text, now if now is not None else time.time(),
             ttl_s if ttl_s is not None else ttl_for(query), session_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def lookup(query: str, tool_input: dict | None = None, db: Path | str | None = None,
           now: float | None = None) -> dict | None:
    """A fresh cached row {query, result, age_s, ttl_s, hits}, or None. Counts the hit."""
    if not normalize(query):
        return None
    now = now if now is not None else time.time()
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT id, query, result, fetched_at, ttl_s, hits FROM web_search_cache WHERE key=?",
            (make_key(query, tool_input),),
        ).fetchone()
        if not row or now - row[3] > row[4]:
            return None
        conn.execute("UPDATE web_search_cache SET hits=hits+1, last_hit=? WHERE id=?", (now, row[0]))
        conn.commit()
        return {"query": row[1], "result": row[2], "age_s": int(now - row[3]),
                "ttl_s": row[4], "hits": row[5] + 1}
    finally:
        conn.close()


def search(text: str, limit: int = 10, db: Path | str | None = None) -> list[dict]:
    """Stored rows whose query or result contains `text`, newest first, fresh or not."""
    like = f"%{text}%"
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT query, mode, fetched_at, ttl_s, hits, substr(result,1,200) FROM web_search_cache"
            " WHERE query LIKE ? OR result LIKE ? ORDER BY fetched_at DESC LIMIT ?", (like, like, limit)
        ).fetchall()
    finally:
        conn.close()
    return [{"query": r[0], "mode": r[1], "fetched_at": r[2], "ttl_s": r[3], "hits": r[4], "snippet": r[5]}
            for r in rows]


def stats(db: Path | str | None = None, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    conn = connect(db)
    try:
        total, fresh, hits = conn.execute(
            "SELECT count(*), coalesce(sum(? - fetched_at <= ttl_s),0), coalesce(sum(hits),0)"
            " FROM web_search_cache", (now,)).fetchone()
    finally:
        conn.close()
    return {"rows": total, "fresh": fresh, "stale": total - fresh, "hits_served": hits}


def purge(days: float, db: Path | str | None = None, now: float | None = None) -> int:
    now = now if now is not None else time.time()
    conn = connect(db)
    try:
        n = conn.execute("DELETE FROM web_search_cache WHERE fetched_at < ?", (now - days * 86400,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _skip_session(session_id: str) -> bool:
    return (session_id or "").startswith(TEST_PREFIXES)


def record_hook(session_id: str, tool_input: dict, tool_response) -> bool:
    """PostToolUse entry: store a WebSearch result. Never raises."""
    try:
        if _skip_session(session_id):
            return False
        return store(tool_input.get("query", ""), tool_input, tool_response, session_id)
    except Exception:
        return False


def cached_reason(session_id: str, tool_input: dict) -> str | None:
    """PreToolUse entry: the text to hand back instead of searching, or None. Never raises."""
    try:
        if _skip_session(session_id):
            return None
        hit = lookup(tool_input.get("query", ""), tool_input)
        if not hit:
            return None
        age_m = hit["age_s"] // 60
        return (
            f"CACHED WebSearch result (fetched {age_m} min ago, TTL {hit['ttl_s'] // 60} min, "
            f"served {hit['hits']}x) for query {hit['query']!r}. The search was skipped. To force a "
            "fresh search, change the query wording or run `uv run python -m src.tools.web_search_cache "
            "purge --days 0` first.\n\n" + hit["result"]
        )
    except Exception:
        return None


# ---- handlers run by `mac hooks web_search_*` (cli-graph mac/groups/hooks.py) ----

def handle_get(query: str, mode: str = "standard", allow_stale: bool = False) -> dict:
    """The fresh cached result for a query, or {"hit": False}. allow_stale also serves an expired row."""
    tool_input = {"mode": mode}
    hit = lookup(query, tool_input)
    if hit:
        return {"hit": True, "stale": False, **hit}
    if allow_stale:
        conn = connect()
        try:
            row = conn.execute("SELECT query, result, fetched_at, ttl_s FROM web_search_cache WHERE key=?",
                               (make_key(query, tool_input),)).fetchone()
        finally:
            conn.close()
        if row:
            return {"hit": True, "stale": True, "query": row[0], "result": row[1],
                    "age_s": int(time.time() - row[2]), "ttl_s": row[3]}
    return {"hit": False, "ttl_s_if_stored": ttl_for(query)}


def handle_put(query: str, result: str, mode: str = "standard", ttl_s: int | None = None) -> dict:
    return {"stored": store(query, {"mode": mode}, result, ttl_s=ttl_s)}


def handle_find(text: str, limit: int = 10) -> dict:
    return {"rows": search(text, limit)}


def handle_stats() -> dict:
    return stats()


def handle_purge(days: float) -> dict:
    return {"deleted": purge(days)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="web_search_cache")
    p.add_argument("--db")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get"); g.add_argument("query"); g.add_argument("--mode", default="standard")
    s = sub.add_parser("search"); s.add_argument("text"); s.add_argument("--limit", type=int, default=10)
    pu = sub.add_parser("put"); pu.add_argument("query"); pu.add_argument("--file", required=True)
    pu.add_argument("--mode", default="standard"); pu.add_argument("--ttl", type=int)
    pg = sub.add_parser("purge"); pg.add_argument("--days", type=float, required=True)
    sub.add_parser("stats")
    a = p.parse_args(argv)
    if a.cmd == "get":
        hit = lookup(a.query, {"mode": a.mode}, a.db)
        print(json.dumps(hit, ensure_ascii=False, indent=2) if hit else "miss")
        return 0 if hit else 1
    if a.cmd == "search":
        print(json.dumps(search(a.text, a.limit, a.db), ensure_ascii=False, indent=2))
    elif a.cmd == "put":
        ok = store(a.query, {"mode": a.mode}, Path(a.file).read_text(), ttl_s=a.ttl, db=a.db)
        print("stored" if ok else "nothing stored")
    elif a.cmd == "purge":
        print(f"deleted {purge(a.days, a.db)}")
    else:
        print(json.dumps(stats(a.db), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
