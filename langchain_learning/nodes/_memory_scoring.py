"""Shared combination-signal scorer for memory retrieval.

Used by LoadMemoriesNode (prompt → memories) and ActivateTaskNode (task → memories).

Signals per row:
  1. tag_overlap    — Jaccard(tokens ∩ tags) × tag_weight
  2. body_overlap   — Jaccard(tokens ∩ body) × body_weight
  3. recency        — multiplier based on updated timestamp

A row with zero tag/body overlap scores 0 and is dropped by min_keyword_score —
there is no baseline weight keeping it eligible (domain used to provide one).

Config loaded from memory_scoring.json (iCloud), mtime-cached.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from langchain_learning.nodes._text_utils import tokenise
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
        "batch_limit":             _src_cfg.memory_batch_limit,
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


def combination_score(
    row: sqlite3.Row,
    tokens: set[str],
    cfg: dict,
) -> float:
    """Score one memory row against a token set using tag/body overlap + recency."""
    if not tokens:
        return 0.0

    tag_tokens  = set(tokenise((row["tags"]  or "").lower()))
    body_tokens = set(tokenise((row["body"]  or "").lower()))

    tag_score  = (len(tokens & tag_tokens)  / max(len(tag_tokens),  1)) * cfg.get("tag_weight", 3.0)
    body_score = (len(tokens & body_tokens) / max(len(tokens), 1))      * cfg.get("body_weight", 1.0)

    if (tag_score + body_score) < cfg.get("min_keyword_score", 0.0):
        return 0.0

    return (tag_score + body_score) * recency_multiplier(row["updated"], cfg)


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


def _link_boosts(conn, cfg: dict, tokens, seed_pool, rows_by_name) -> dict | None:
    """Boost graph neighbours from memory_links. None if the table is unusable.

    Neighbours are fetched by id, so they need not be inside the batch_limit window.
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

    direct = {oid: combination_score(r, tokens, cfg) for oid, r in rows.items()}
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
) -> list[dict]:
    """Score all candidate memories and return top-N by combination signal.

    After the initial keyword scoring pass, applies a graph-neighbour
    boost: each high-scoring memory lifts its memory_links neighbours by
    related_boost_factor × seed_score (see _link_boosts), demotes a memory whose
    superseder is also a candidate, and tags returned contradicting pairs with
    `conflicts_with`. related_source="related" (or an unusable memory_links)
    keeps the legacy `related`-column boost.
    """
    cfg = load_scoring_cfg()
    batch_limit = cfg.get("batch_limit", 500)
    rows: list[sqlite3.Row] = conn.execute(
        f"SELECT {_NEIGHBOUR_COLS} FROM memories "
        "ORDER BY updated DESC LIMIT ?",
        (batch_limit,),
    ).fetchall()

    rows_by_name: dict[str, sqlite3.Row] = {row["name"]: row for row in rows}

    scored: dict[str, tuple[float, dict]] = {}
    for row in rows:
        s = combination_score(row, tokens, cfg)
        if s > 0:
            scored[row["name"]] = (s, dict(row))

    n = top_n if top_n is not None else cfg.get("top_n", 5)
    seed_pool = sorted(scored.items(), key=lambda x: -x[1][0])[: n * 2]

    boosts = None
    if cfg.get("related_source", "links") == "links":
        boosts = _link_boosts(conn, cfg, tokens, seed_pool, rows_by_name)
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
    if graph_ok:
        _mark_conflicts(conn, top)
    return top


def record_memory_hits(names: list[str]) -> None:
    """Increment hit_count/last_hit for each memory name actually returned
    by score_memories(). Best-effort — errors are logged, never raised, so a
    write failure can't break prompt injection (mirrors record_memories()'s
    fire-and-forget instrumentation pattern in hooks/server_memory.py).

    Opens its own short-lived read-write connection — score_memories() itself
    reads via a read-only connection (CombinationSignalRetriever.retrieve()),
    so this is intentionally a separate write path, not folded into scoring.
    """
    if not names:
        return
    from src.config import config as _src_cfg

    try:
        conn = sqlite3.connect(_src_cfg.memory_db)
        conn.executemany(
            "UPDATE memories SET hit_count = COALESCE(hit_count, 0) + 1, "
            "last_hit = CURRENT_TIMESTAMP WHERE name = ?",
            [(name,) for name in names],
        )
        conn.commit()
        conn.close()
        _log.debug("[record_memory_hits] incremented=%d names=%s", len(names), names)
    except Exception as exc:
        _log.warning("[record_memory_hits] failed (hit-count tracking degraded): %s", exc)
