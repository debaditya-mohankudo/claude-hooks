"""Audit log of memory retrieval — one row per prompt, so scoring can be tuned from evidence.

Each row keeps the prompt, its tokens, the mac areas carried over from earlier
prompts, the top candidates (name, score, matched terms) and the memories actually
injected. `verdict` starts NULL; `mark()` fills it per injected memory with
"useful" or "noise". `report()` turns verdicts into per-memory precision and the
terms that most often caused noise, which is what to fix in tags or thresholds.

Stored in its own file (append-only, no eviction) so server_memory's capped store stays capped.
Every write is fail-open: the hook path must never notice an audit failure.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

from src.logger import get_logger

_log = get_logger(__name__)

AUDIT_DB = Path.home() / ".claude" / "memory_audit.sqlite"
MEMORY_DB: Path | None = None  # None -> config.memory_db; tests point it at a temp file

_DDL = """CREATE TABLE IF NOT EXISTS memory_audit (
    id          INTEGER PRIMARY KEY,
    ts          REAL,
    session_id  TEXT,
    prompt      TEXT,
    tokens      TEXT,   -- JSON list: query tokens actually scored (own + carried)
    carried     TEXT,   -- JSON list: mac areas added from earlier prompts
    candidates  TEXT,   -- JSON list of {name, score, hits}
    injected    TEXT,   -- JSON list of memory names returned
    verdict     TEXT,   -- JSON {name: 'useful'|'noise'}; NULL until marked
    skipped     TEXT    -- why retrieval did not run ('short'|'machine'); NULL when it did
)"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or AUDIT_DB), timeout=2)
    conn.execute(_DDL)
    if "skipped" not in {r[1] for r in conn.execute("PRAGMA table_info(memory_audit)")}:
        conn.execute("ALTER TABLE memory_audit ADD COLUMN skipped TEXT")
    return conn


def record(session_id: str, prompt: str, tokens, carried, candidates, injected,
           path: Path | None = None, skipped: str | None = None) -> None:
    """Append one audit row. Never raises. Rows with no session id (direct/test calls) are skipped."""
    if not session_id:
        return
    try:
        conn = _connect(path)
        try:
            conn.execute(
                "INSERT INTO memory_audit (ts, session_id, prompt, tokens, carried, candidates, injected, skipped) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), session_id, prompt, json.dumps(sorted(tokens)), json.dumps(list(carried)),
                 json.dumps(candidates), json.dumps(list(injected)), skipped),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _log.warning("[memory_audit] record failed (audit degraded): %s", exc)


def recent(limit: int = 20, unmarked_only: bool = False, path: Path | None = None) -> list[dict]:
    """Newest-first audit rows as dicts (JSON columns decoded)."""
    conn = _connect(path)
    try:
        q = "SELECT id, ts, session_id, prompt, tokens, carried, candidates, injected, verdict, skipped FROM memory_audit"
        if unmarked_only:
            q += " WHERE verdict IS NULL AND skipped IS NULL"
        rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    keys = ("id", "ts", "session_id", "prompt", "tokens", "carried", "candidates", "injected", "verdict", "skipped")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        for k in ("tokens", "carried", "candidates", "injected", "verdict"):
            d[k] = json.loads(d[k]) if d[k] else ([] if k != "verdict" else None)
        out.append(d)
    return out


def mark(audit_id: int, verdicts: dict[str, str], path: Path | None = None) -> None:
    """Set verdicts ({memory name: 'useful'|'noise'}) on a row, merging with earlier ones."""
    bad = {v for v in verdicts.values() if v not in ("useful", "noise")}
    if bad:
        raise ValueError(f"verdict must be 'useful' or 'noise', got {sorted(bad)}")
    conn = _connect(path)
    try:
        row = conn.execute("SELECT injected, verdict FROM memory_audit WHERE id=?", (audit_id,)).fetchone()
        if row is None:
            raise KeyError(f"no audit row {audit_id}")
        unknown = set(verdicts) - set(json.loads(row[0] or "[]"))
        if unknown:
            raise ValueError(f"not injected on that prompt: {sorted(unknown)}")
        old = json.loads(row[1]) if row[1] else {}
        merged = {**old, **verdicts}
        # Only a changed verdict moves the counter, so re-marking never double counts.
        # Counters first: if that write fails the verdict stays unset and can be retried.
        _bump_mismatch({n: 1 if v == "noise" else -1 for n, v in verdicts.items() if old.get(n) != v})
        conn.execute("UPDATE memory_audit SET verdict=? WHERE id=?", (json.dumps(merged), audit_id))
        conn.commit()
    finally:
        conn.close()


def _bump_mismatch(deltas: dict[str, int]) -> None:
    """Add each delta to memories.mismatch_count (never below 0). `updated` is left
    alone, so a mark does not earn the memory a recency boost."""
    if not deltas:
        return
    from langchain_learning.memory_conn import memory_connection
    from src.config import config
    from src.tools.memory import _ensure_schema

    with memory_connection(MEMORY_DB or config.memory_db) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'memories'").fetchone():
            return
        _ensure_schema(conn)
        conn.executemany(
            "UPDATE memories SET mismatch_count = MAX(0, mismatch_count + ?) WHERE name = ?",
            [(d, n) for n, d in deltas.items()],
        )


def report(path: Path | None = None) -> dict:
    """Precision per memory and the matched terms behind noise, from marked rows."""
    per: dict[str, Counter] = defaultdict(Counter)
    noise_terms: Counter = Counter()
    carried_noise = carried_total = 0
    for row in recent(limit=100000, path=path):
        if not row["verdict"]:
            continue
        hits_by_name = {c["name"]: c.get("hits", []) for c in row["candidates"]}
        for name, v in row["verdict"].items():
            per[name][v] += 1
            if v == "noise":
                noise_terms.update(hits_by_name.get(name, []))
            if row["carried"]:
                carried_total += 1
                carried_noise += v == "noise"
    memories = {
        n: {"useful": c["useful"], "noise": c["noise"], "precision": round(c["useful"] / (c["useful"] + c["noise"]), 2)}
        for n, c in per.items()
    }
    return {
        "marked_memories": sum(sum(c.values()) for c in per.values()),
        "memories": dict(sorted(memories.items(), key=lambda kv: kv[1]["precision"])),
        "noise_terms": noise_terms.most_common(10),
        "carried_noise_rate": round(carried_noise / carried_total, 2) if carried_total else None,
    }
