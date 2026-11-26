"""Shared BM25 scorer for memory retrieval.

Used by LoadMemoriesNode (prompt → memories) and ActivateTaskNode (task → memories).

Signals per row:
  1. BM25 over the memory's tags (IDF + length normalisation) × tag_weight
  2. recency — multiplier based on updated timestamp

The BM25 index covers every memory so IDF reflects the whole corpus. It is
cached at module level and rebuilt only when the (id, updated, tags) signature
of the table changes, so a prompt does not re-tokenise the corpus.
A row with no tag match scores 0 and is dropped by min_keyword_score.

Match filter: a memory only scores when the prompt hits at least
`min_distinct_terms` (2) distinct tag/slug terms of it, or hits one term that
appears in at most `rare_term_df` (8) memories. A lone common word ("check",
"idea") no longer pulls in an unrelated memory. A one-word prompt needs one hit.

Config loaded from memory_scoring.json (iCloud), mtime-cached.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from rank_bm25 import BM25Plus

from langchain_learning.nodes._text_utils import _STOP_WORDS
from src.config import config as _src_cfg
from src.logger import get_logger

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Config — mtime-cached
# ---------------------------------------------------------------------------

_scoring_cfg: dict = {}
_scoring_cfg_mtime: float = 0.0

def _defaults_from_config() -> dict:
    """Build scalar defaults from src.config (env-overridable)."""
    return {
        "top_n":                   _src_cfg.memory_top_n,
        "tag_weight":              _src_cfg.memory_tag_weight,
        "body_weight":             _src_cfg.memory_body_weight,
        "recency_boost":           _src_cfg.memory_recency_boost,
        "recency_penalty":         _src_cfg.memory_recency_penalty,
        "recency_boost_days":      _src_cfg.memory_recency_boost_days,
        "recency_penalty_days":    _src_cfg.memory_recency_penalty_days,
        "min_keyword_score":       _src_cfg.memory_min_keyword_score,
        "related_boost_factor":    0.15,
        "related_max_neighbours":  2,
        "related_source":          "links",   # "links" = memory_links edges; "related" = legacy column
        "related_target_cap":      0.5,       # a target's summed boost <= this x the top seed score
        "supersedes_penalty":      0.3,       # multiplier on a memory whose superseder is also a candidate
        "min_distinct_terms":      2,         # distinct prompt terms a memory must hit ...
        "rare_term_df":            8,         # ... unless one hit term is in <= this many memories
    }


def load_scoring_cfg() -> dict:
    """Return scoring config: scalars from src.config, structured data from JSON (mtime-cached)."""
    global _scoring_cfg, _scoring_cfg_mtime
    path = _src_cfg.memory_scoring_json
    try:
        mtime = path.stat().st_mtime
        if mtime != _scoring_cfg_mtime:
            json.loads(path.read_text())  # validated, no structured domain fields left to merge
            _scoring_cfg = _defaults_from_config()
            _scoring_cfg_mtime = mtime
            _log.info("[memory_scoring] config reloaded from %s", path.name)
    except Exception:
        if not _scoring_cfg:
            _scoring_cfg = _defaults_from_config()
    return _scoring_cfg


# ---------------------------------------------------------------------------
# Scoring primitives
# ---------------------------------------------------------------------------

def recency_multiplier(updated_str: str | None, cfg: dict) -> float:
    if not updated_str:
        return 1.0
    try:
        updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - updated).days
        if age_days <= cfg.get("recency_boost_days", 30):
            return cfg.get("recency_boost", 1.2)
        if age_days >= cfg.get("recency_penalty_days", 180):
            return cfg.get("recency_penalty", 0.8)
        return 1.0
    except Exception:
        return 1.0


def _tag_terms(tags: str | None) -> list[str]:
    """Tag tokens as a list (duplicates kept so BM25 sees term frequency and length)."""
    return [t for t in re.findall(r"[a-z]{3,}", (tags or "").lower()) if t not in _STOP_WORDS]


_bm25_cache: dict = {"sig": None, "bm25": None, "pos": {}, "terms": [], "df": Counter()}


def _bm25_index(conn: sqlite3.Connection) -> tuple[BM25Plus | None, dict[int, int]]:
    """Return (index, id -> position), rebuilt only when the table's tags/updated change.

    The cache also holds each document's term set and the term -> document-frequency
    counter that `bm25_scores` uses for the match filter."""
    rows = conn.execute("SELECT id, tags, updated, name FROM memories ORDER BY id").fetchall()
    sig = hash(tuple((r[0], r[1], r[2], r[3]) for r in rows))
    if _bm25_cache["sig"] != sig:
        corpus = [(_tag_terms(r[1]) + _tag_terms(re.sub(r"[-_]", " ", r[3] or ""))) or ["_"] for r in rows]  # BM25 needs non-empty docs
        _bm25_cache.update(
            sig=sig,
            # BM25Plus idf=log((N+1)/n) stays > 0 on tiny corpora; delta=0 keeps
            # a doc that lacks the term at exactly 0 (Okapi's idf hits 0/negative).
            bm25=BM25Plus(corpus, delta=0) if corpus else None,
            pos={r[0]: i for i, r in enumerate(rows)},
            terms=[set(d) - {"_"} for d in corpus],
            df=Counter(t for d in corpus for t in set(d) if t != "_"),
        )
        _log.info("[memory_scoring] BM25 index rebuilt docs=%d", len(rows))
    return _bm25_cache["bm25"], _bm25_cache["pos"]


def _passes_match_filter(hits: set[str], n_tokens: int, df: Counter, cfg: dict) -> bool:
    """True when `hits` (query terms found in one memory) are enough to score it."""
    need = min(cfg.get("min_distinct_terms", 2), n_tokens)
    if len(hits) >= need:
        return True
    rare = cfg.get("rare_term_df", 8)
    return any(df[t] <= rare for t in hits)


def bm25_scores(conn: sqlite3.Connection, tokens: set[str]) -> dict[int, float]:
    """BM25 score of the query tokens against every memory's tags and slug, keyed by memory id.

    A memory that fails the match filter (see module docstring) scores 0."""
    if not tokens:
        return {}
    bm25, pos = _bm25_index(conn)
    if bm25 is None:
        return {}
    cfg = load_scoring_cfg()
    terms, df = _bm25_cache["terms"], _bm25_cache["df"]
    raw = bm25.get_scores(sorted(tokens))
    return {
        mid: float(raw[i]) if _passes_match_filter(terms[i] & tokens, len(tokens), df, cfg) else 0.0
        for mid, i in pos.items()
    }


def area_tokens(memories: list[dict]) -> list[str]:
    """`mac-<area>` tag areas of the given memories, most frequent first (order-stable)."""
    counts: Counter = Counter()
    for m in memories:
        for area in re.findall(r"\bmac-([a-z]{3,})", (m.get("tags") or "").lower()):
            counts[area] += 1
    return [a for a, _ in counts.most_common()]


def combination_score(row: sqlite3.Row, scores: dict[int, float], cfg: dict) -> float:
    """Final score for one memory: BM25(tags) × tag_weight × recency, or 0 below the floor."""
    s = scores.get(row["id"], 0.0) * cfg.get("tag_weight", 3.0)
    if s <= 0 or s < cfg.get("min_keyword_score", 0.0):
        return 0.0
    return s * recency_multiplier(row["updated"], cfg)


_NEIGHBOUR_COLS = "id, name, type, tags, body, related, updated"
_BOOST_TYPES = {"relates_to", "extends", "learned_from"}


def _related_column_boosts(cfg: dict, seed_pool, rows_by_name) -> dict:
    """Legacy boost: each seed lifts the first N slugs of its `related` column."""
    boost_factor   = cfg.get("related_boost_factor", 0.15)
    max_neighbours = cfg.get("related_max_neighbours", 2)
    out: dict[str, tuple[float, sqlite3.Row]] = {}
    for seed_name, (seed_score, _) in seed_pool:
        seed_row = rows_by_name.get(seed_name)
        if not seed_row:
            continue
        slugs = [s.strip() for s in (seed_row["related"] or "").split(",") if s.strip()]
        for slug in slugs[:max_neighbours]:
            boost = boost_factor * seed_score
            if boost > 0 and slug in rows_by_name:
                prev = out.get(slug, (0.0, None))[0]
                out[slug] = (prev + boost, rows_by_name[slug])
    return out


def _link_boosts(conn, cfg: dict, scores, seed_pool, rows_by_name) -> dict | None:
    """Boost graph neighbours from memory_links. None if the table is unusable.

    Neighbours are fetched by id.
    Each seed keeps its best-matching neighbours (not the first in a string), and a
    target's summed boost is capped so a hub memory cannot swamp the top-N.
    Edge rules: relates_to/extends/learned_from boost either way; supersedes boosts
    only the superseder (new) when the old memory is the seed; contradicts never boosts.
    """
    seed_ids = {rows_by_name[name]["id"]: score for name, (score, _) in seed_pool if name in rows_by_name}
    if not seed_ids:
        return {}
    ph = ",".join("?" * len(seed_ids))
    try:
        edges = conn.execute(
            f"SELECT src_id, dst_id, type FROM memory_links WHERE src_id IN ({ph}) OR dst_id IN ({ph})",
            [*seed_ids, *seed_ids],
        ).fetchall()
        cands: dict[int, set[int]] = {sid: set() for sid in seed_ids}
        for src, dst, etype in edges:
            for seed, other in ((src, dst), (dst, src)):
                if seed not in seed_ids or other == seed:
                    continue
                if etype in _BOOST_TYPES or (etype == "supersedes" and seed == dst):
                    cands[seed].add(other)
        other_ids = {o for c in cands.values() for o in c}
        if not other_ids:
            return {}
        qs = ",".join("?" * len(other_ids))
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT {_NEIGHBOUR_COLS} FROM memories WHERE id IN ({qs})", list(other_ids))}
    except sqlite3.Error as exc:
        _log.warning("[memory_scoring] memory_links unusable, using related column: %s", exc)
        return None

    direct = {oid: combination_score(r, scores, cfg) for oid, r in rows.items()}
    factor = cfg.get("related_boost_factor", 0.15)
    max_n = cfg.get("related_max_neighbours", 2)
    cap = cfg.get("related_target_cap", 0.5) * max(seed_ids.values())
    out: dict[str, tuple[float, sqlite3.Row]] = {}
    for sid, others in cands.items():
        ranked = sorted(others, key=lambda o: rows[o]["updated"] or "", reverse=True)
        ranked.sort(key=lambda o: -direct[o])  # stable: ties stay newest-first
        for oid in ranked[:max_n]:
            boost = factor * seed_ids[sid]
            if boost > 0:
                name = rows[oid]["name"]
                out[name] = (min(out.get(name, (0.0, None))[0] + boost, cap), rows[oid])
    return out


def _demote_superseded(conn, cfg: dict, scored: dict) -> None:
    """Multiply the score of a memory whose superseder is also a candidate."""
    by_id = {m["id"]: name for name, (_, m) in scored.items() if m.get("id") is not None}
    if len(by_id) < 2:
        return
    ph = ",".join("?" * len(by_id))
    try:
        pairs = conn.execute(
            f"SELECT dst_id FROM memory_links WHERE type='supersedes' "
            f"AND src_id IN ({ph}) AND dst_id IN ({ph})", [*by_id, *by_id],
        ).fetchall()
    except sqlite3.Error:
        return
    for (old_id,) in pairs:
        old = by_id[old_id]
        s, m = scored[old]
        scored[old] = (s * cfg.get("supersedes_penalty", 0.3), m)


def _mark_conflicts(conn, top: list[dict]) -> None:
    """Tag returned memories joined by a contradicts edge with `conflicts_with`."""
    by_id = {m["id"]: m for m in top if m.get("id") is not None}
    if len(by_id) < 2:
        return
    ph = ",".join("?" * len(by_id))
    try:
        pairs = conn.execute(
            f"SELECT src_id, dst_id FROM memory_links WHERE type='contradicts' "
            f"AND src_id IN ({ph}) AND dst_id IN ({ph})", [*by_id, *by_id],
        ).fetchall()
    except sqlite3.Error:
        return
    for a, b in pairs:
        by_id[a].setdefault("conflicts_with", []).append(by_id[b]["name"])
        by_id[b].setdefault("conflicts_with", []).append(by_id[a]["name"])


def score_memories(
    tokens: set[str],
    conn: sqlite3.Connection,
    top_n: int | None = None,
    trace: list | None = None,
) -> list[dict]:
    """Score every memory and return top-N by combination signal.

    When `trace` is a list it receives one dict per top candidate
    ({name, score, hits}) for the audit log; it never changes the result.

    After the initial keyword scoring pass, applies a graph-neighbour
    boost: each high-scoring memory lifts its memory_links neighbours by
    related_boost_factor × seed_score (see _link_boosts), demotes a memory whose
    superseder is also a candidate, and tags returned contradicting pairs with
    `conflicts_with`. related_source="related" (or an unusable memory_links)
    keeps the legacy `related`-column boost.
    """
    cfg = load_scoring_cfg()
    # No row limit: a window ordered by `updated` silently stops scoring old memories.
    # The ORDER BY keeps score ties newest-first.
    rows: list[sqlite3.Row] = conn.execute(
        f"SELECT {_NEIGHBOUR_COLS} FROM memories ORDER BY updated DESC"
    ).fetchall()

    rows_by_name: dict[str, sqlite3.Row] = {row["name"]: row for row in rows}
    scores_by_id = bm25_scores(conn, tokens)

    scored: dict[str, tuple[float, dict]] = {}
    for row in rows:
        s = combination_score(row, scores_by_id, cfg)
        if s > 0:
            scored[row["name"]] = (s, dict(row))

    n = top_n if top_n is not None else cfg.get("top_n", 5)
    seed_pool = sorted(scored.items(), key=lambda x: -x[1][0])[: n * 2]

    boosts = None
    if cfg.get("related_source", "links") == "links":
        boosts = _link_boosts(conn, cfg, scores_by_id, seed_pool, rows_by_name)
    graph_ok = boosts is not None
    if boosts is None:  # legacy source, or memory_links unusable
        boosts = _related_column_boosts(cfg, seed_pool, rows_by_name)
    for name, (boost, row) in boosts.items():
        if name in scored:
            prev_score, mem = scored[name]
            scored[name] = (prev_score + boost, mem)
        else:
            scored[name] = (boost, dict(row))

    if graph_ok:
        _demote_superseded(conn, cfg, scored)

    all_scored = sorted(scored.values(), key=lambda x: -x[0])
    top = [m for _, m in all_scored[:n]]
    if trace is not None:
        _fill_trace(trace, all_scored[: n * 2], tokens)
    if graph_ok:
        _mark_conflicts(conn, top)
    return top


def _fill_trace(trace: list, ranked: list[tuple[float, dict]], tokens: set[str]) -> None:
    """Append {name, score, hits} for each ranked candidate (hits = query terms in its tags/slug)."""
    terms, pos = _bm25_cache["terms"], _bm25_cache["pos"]
    for score, m in ranked:
        i = pos.get(m.get("id"))
        hits = sorted(terms[i] & tokens) if i is not None and i < len(terms) else []
        trace.append({"name": m["name"], "score": round(score, 3), "hits": hits})


def record_memory_hits(names: list[str]) -> None:
    """Increment hit_count/last_hit for each memory name actually returned
    by score_memories(). Best-effort — errors are logged, never raised, so a
    write failure can't break prompt injection (mirrors record_memories()'s
    fire-and-forget instrumentation pattern in hooks/server_memory.py).

    Writes through the process-wide memory_connection(), the same connection
    CombinationSignalRetriever.retrieve() reads through: a short-lived open per
    call paid ~300 ms of SQLCipher key derivation every prompt (task:bc39e90e).
    It stays a separate call after scoring, not folded into it.
    """
    if not names:
        return
    from src.config import config as _src_cfg
    from langchain_learning.memory_conn import memory_connection

    try:
        with memory_connection(_src_cfg.memory_db) as conn:
            conn.executemany(
                "UPDATE memories SET hit_count = COALESCE(hit_count, 0) + 1, "
                "last_hit = CURRENT_TIMESTAMP WHERE name = ?",
                [(name,) for name in names],
            )
        _log.debug("[record_memory_hits] incremented=%d names=%s", len(names), names)
    except Exception as exc:
        _log.warning("[record_memory_hits] failed (hit-count tracking degraded): %s", exc)
