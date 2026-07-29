"""Shared combination-signal scorer for memory retrieval.

Used by LoadMemoriesNode (prompt → memories) and ActivateTaskNode (task → memories).

Signals per row:
  1. domain_weight  — project=2.0 | explicit weight per domain | 0 (skip)
  2. keyword_boost  — prompt tokens overlap with domain_keywords → +boost (cross-domain only)
  3. tag_overlap    — Jaccard(tokens ∩ tags) × tag_weight
  4. body_overlap   — Jaccard(tokens ∩ body) × body_weight
  5. recency        — multiplier based on updated timestamp

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
        "domain_keyword_boost":    _src_cfg.memory_domain_keyword_boost,
        # structured defaults (overridden by JSON)
        "domain_weights":          {"project": 2.0, "global": 0.5, "coding-best-practices": 0.3},
        "domain_keywords":         {},
        "combination_signals":     {},
        "related_boost_factor":    0.15,
        "related_max_neighbours":  2,
    }


def load_scoring_cfg() -> dict:
    """Return scoring config: scalars from src.config, structured data from JSON (mtime-cached)."""
    global _scoring_cfg, _scoring_cfg_mtime
    path = _src_cfg.memory_scoring_json
    try:
        mtime = path.stat().st_mtime
        if mtime != _scoring_cfg_mtime:
            json_data = json.loads(path.read_text())
            # Merge: config scalars as base, JSON structured fields on top
            _scoring_cfg = {**_defaults_from_config(), **{
                k: json_data[k] for k in ("domain_weights", "domain_keywords", "combination_signals")
                if k in json_data
            }}
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
    project_domain: str | None,
    cfg: dict,
) -> float:
    """Score one memory row against a token set using combination signals."""
    domain = row["domain"] or "global"
    domain_weights: dict = cfg.get("domain_weights", {"project": 2.0, "global": 0.5})

    if domain == project_domain:
        domain_weight = domain_weights.get("project", 2.0)
    else:
        domain_weight = domain_weights.get(domain, 0.0)

    # Cross-domain keyword boost — only when CWD didn't already match this domain
    if tokens and domain != project_domain:
        domain_kws = set(cfg.get("domain_keywords", {}).get(domain, []))
        if domain_kws and (tokens & domain_kws):
            domain_weight += cfg.get("domain_keyword_boost", 0.8)

    # Combination signals — pairs of tokens that together boost a domain
    for combo, bonus in cfg.get("combination_signals", {}).get(domain, []):
        if set(combo).issubset(tokens):
            domain_weight += bonus

    if domain_weight == 0.0:
        return 0.0

    if not tokens:
        return domain_weight * recency_multiplier(row["updated"], cfg)

    tag_tokens  = set(tokenise((row["tags"]  or "").lower()))
    body_tokens = set(tokenise((row["body"]  or "").lower()))

    tag_score  = (len(tokens & tag_tokens)  / max(len(tag_tokens),  1)) * cfg.get("tag_weight", 3.0)
    body_score = (len(tokens & body_tokens) / max(len(tokens), 1))      * cfg.get("body_weight", 1.0)

    if (tag_score + body_score) < cfg.get("min_keyword_score", 0.0):
        return 0.0

    return (domain_weight + tag_score + body_score) * recency_multiplier(row["updated"], cfg)


def score_memories(
    tokens: set[str],
    project_domain: str | None,
    conn: sqlite3.Connection,
    top_n: int | None = None,
) -> list[dict]:
    """Score all candidate memories and return top-N by combination signal.

    After the initial keyword+domain scoring pass, applies a graph-neighbour
    boost: each high-scoring memory lifts its `related` siblings by
    related_boost_factor × seed_score (additive, capped to related_max_neighbours
    per seed). This makes the retriever concept-graph-aware without embeddings.
    """
    cfg = load_scoring_cfg()
    batch_limit = cfg.get("batch_limit", 500)
    # Per-domain, not aggregate: a flat LIMIT across the whole table would let one
    # large domain (or overall growth) silently starve every other domain's rows
    # out of the candidate pool once the total row count crosses the cap. Fetching
    # up to batch_limit per domain keeps each domain's quota independent of the
    # others, and ORDER BY updated DESC means any truncation drops the stalest
    # rows in that domain rather than an arbitrary/oldest-by-rowid slice.
    domains = [r[0] for r in conn.execute("SELECT DISTINCT domain FROM memories").fetchall()]
    rows: list[sqlite3.Row] = []
    for d in domains:
        rows.extend(conn.execute(
            "SELECT name, type, domain, tags, body, related, updated FROM memories "
            "WHERE domain IS ? ORDER BY updated DESC LIMIT ?",
            (d, batch_limit),
        ).fetchall())

    rows_by_name: dict[str, sqlite3.Row] = {row["name"]: row for row in rows}

    scored: dict[str, tuple[float, dict]] = {}
    for row in rows:
        s = combination_score(row, tokens, project_domain, cfg)
        if s > 0:
            scored[row["name"]] = (s, dict(row))

    # Graph-neighbour boost — seeds are 2×top_n highest direct scorers
    n = top_n if top_n is not None else cfg.get("top_n", 5)
    boost_factor   = cfg.get("related_boost_factor", 0.15)
    max_neighbours = cfg.get("related_max_neighbours", 2)
    seed_pool = sorted(scored.items(), key=lambda x: -x[1][0])[: n * 2]

    for seed_name, (seed_score, _) in seed_pool:
        seed_row = rows_by_name.get(seed_name)
        if not seed_row:
            continue
        slugs = [s.strip() for s in (seed_row["related"] or "").split(",") if s.strip()]
        for slug in slugs[:max_neighbours]:
            boost = boost_factor * seed_score
            if boost <= 0:
                continue
            if slug in scored:
                prev_score, mem = scored[slug]
                scored[slug] = (prev_score + boost, mem)
            elif slug in rows_by_name:
                scored[slug] = (boost, dict(rows_by_name[slug]))

    all_scored = sorted(scored.values(), key=lambda x: -x[0])
    return [m for _, m in all_scored[:n]]


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
