"""Tests for langchain_learning/nodes/_memory_scoring.py — combination signal + related boost."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from langchain_learning.nodes._memory_scoring import (
    record_memory_hits,
    score_memories,
    score_repo_memories,
)
from src.db.schema import MEMORIES_DDL

_CFG = {
    "top_n": 5,
    "batch_limit": 500,
    "tag_weight": 3.0,
    "body_weight": 1.0,
    "recency_boost": 1.2,
    "recency_penalty": 0.8,
    "recency_boost_days": 30,
    "recency_penalty_days": 180,
    "min_keyword_score": 0.0,
    "domain_keyword_boost": 0.8,
    "domain_weights": {"project": 2.0, "global": 0.5},
    "domain_keywords": {},
    "combination_signals": {},
    "related_boost_factor": 0.15,
    "related_max_neighbours": 2,
}


@pytest.fixture
def mem_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    con = sqlite3.connect(tmp.name)
    con.row_factory = sqlite3.Row
    con.executescript(MEMORIES_DDL)
    yield con
    con.close()


def _insert(con, name, domain="claude-hooks", tags="", body="", related=""):
    con.execute(
        "INSERT INTO memories (name, type, domain, tags, body, related) VALUES (?,?,?,?,?,?)",
        (name, "project", domain, tags, body, related),
    )
    con.commit()


def test_batch_limit_is_per_domain_not_aggregate(mem_conn):
    """A small domain's memories must not be starved out once a large domain
    alone exceeds batch_limit — the LIMIT applies per domain, not across the
    whole table."""
    cfg = {**_CFG, "batch_limit": 3}
    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=cfg):
        for i in range(10):
            _insert(mem_conn, f"big-domain-mem-{i}", domain="big-domain", tags="widget", body="widget")
        _insert(mem_conn, "small-domain-mem", domain="small-domain", tags="widget", body="widget")

        results = score_memories({"widget"}, "small-domain", mem_conn, top_n=20)
        names = {m["name"] for m in results}
        assert "small-domain-mem" in names


def test_high_scorer_boosts_related_neighbour(mem_conn):
    """A memory with no keyword match should appear if linked via related to a high scorer."""
    _insert(mem_conn, "gate-framework", tags="gate framework prereq", body="Gate ABC pattern", related="gate-prereq-tracking")
    _insert(mem_conn, "gate-prereq-tracking", tags="obscure-unrelated-zzz", body="obscure-unrelated-zzz")

    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=_CFG):
        results = score_memories({"gate", "framework"}, "claude-hooks", mem_conn, top_n=5)

    names = [m["name"] for m in results]
    assert "gate-framework" in names, "seed should be in results"
    assert "gate-prereq-tracking" in names, "related neighbour should be boosted into results"


def test_neighbour_boost_is_additive(mem_conn):
    """A neighbour that already scores on its own should score higher after boost."""
    _insert(mem_conn, "seed", tags="gate", body="gate framework", related="neighbour")
    _insert(mem_conn, "neighbour", tags="gate", body="gate prereq")

    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=_CFG):
        results_with_related = score_memories({"gate"}, "claude-hooks", mem_conn, top_n=5)

    # Remove related from seed to get baseline neighbour score
    mem_conn.execute("UPDATE memories SET related='' WHERE name='seed'")
    mem_conn.commit()

    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=_CFG):
        results_without_related = score_memories({"gate"}, "claude-hooks", mem_conn, top_n=5)

    def get_rank(results, name):
        names = [m["name"] for m in results]
        return names.index(name) if name in names else 999

    # neighbour should rank higher (lower index) when boost is applied
    assert get_rank(results_with_related, "neighbour") <= get_rank(results_without_related, "neighbour")


def test_max_neighbours_per_seed_respected(mem_conn):
    """Only related_max_neighbours (2) neighbours per seed should be boosted.

    Noise memories use domain='unknown' (weight=0) so they only appear via boost.
    """
    _insert(mem_conn, "seed", tags="gate", body="gate", related="n1,n2,n3")
    _insert(mem_conn, "n1", domain="unknown", tags="zzz", body="zzz")
    _insert(mem_conn, "n2", domain="unknown", tags="zzz", body="zzz")
    _insert(mem_conn, "n3", domain="unknown", tags="zzz", body="zzz")

    cfg = {**_CFG, "related_max_neighbours": 2, "top_n": 10}
    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=cfg):
        results = score_memories({"gate"}, "claude-hooks", mem_conn, top_n=10)

    names = [m["name"] for m in results]
    boosted = [n for n in ["n1", "n2", "n3"] if n in names]
    assert len(boosted) == 2, f"expected 2 boosted neighbours, got {boosted}"


def test_zero_boost_factor_disables_graph(mem_conn):
    """related_boost_factor=0 should not surface zero-direct-scoring neighbours.

    Neighbour uses domain='unknown' (weight=0) so it only appears via boost.
    """
    _insert(mem_conn, "seed", tags="gate", body="gate", related="silent-neighbour")
    _insert(mem_conn, "silent-neighbour", domain="unknown", tags="zzz", body="zzz")

    cfg = {**_CFG, "related_boost_factor": 0.0}
    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=cfg):
        results = score_memories({"gate"}, "claude-hooks", mem_conn, top_n=5)

    names = [m["name"] for m in results]
    assert "silent-neighbour" not in names


# ---------------------------------------------------------------------------
# record_memory_hits — hit_count/last_hit instrumentation (task:fb7d3777-adjacent:
# found while investigating that task that hit_count/last_hit existed on the
# live DB schema and in CLAUDE.md's docs, but nothing anywhere ever wrote to
# them — this wires up the missing write side rather than dropping the columns)
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_db_path(tmp_path):
    """record_memory_hits opens its own connection by path (not a fixture
    conn), so it needs a real file, unlike mem_conn above."""
    db_path = tmp_path / "MEMORY.sqlite"
    con = sqlite3.connect(str(db_path))
    con.executescript(MEMORIES_DDL)
    con.execute("INSERT INTO memories (name, type, domain) VALUES ('alpha', 'project', 'claude-hooks')")
    con.execute("INSERT INTO memories (name, type, domain) VALUES ('beta', 'project', 'claude-hooks')")
    con.commit()
    con.close()
    return db_path


def test_record_memory_hits_increments_count_and_sets_last_hit(mem_db_path):
    with patch("src.config.config", memory_db=mem_db_path):
        record_memory_hits(["alpha"])

    con = sqlite3.connect(str(mem_db_path))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT hit_count, last_hit FROM memories WHERE name='alpha'").fetchone()
    con.close()
    assert row["hit_count"] == 1
    assert row["last_hit"] is not None


def test_record_memory_hits_is_cumulative_across_calls(mem_db_path):
    with patch("src.config.config", memory_db=mem_db_path):
        record_memory_hits(["alpha"])
        record_memory_hits(["alpha"])
        record_memory_hits(["alpha"])

    con = sqlite3.connect(str(mem_db_path))
    row = con.execute("SELECT hit_count FROM memories WHERE name='alpha'").fetchone()
    con.close()
    assert row[0] == 3


def test_record_memory_hits_only_touches_named_rows(mem_db_path):
    with patch("src.config.config", memory_db=mem_db_path):
        record_memory_hits(["alpha"])

    con = sqlite3.connect(str(mem_db_path))
    row = con.execute("SELECT hit_count FROM memories WHERE name='beta'").fetchone()
    con.close()
    assert row[0] == 0


def test_record_memory_hits_handles_multiple_names_in_one_call(mem_db_path):
    with patch("src.config.config", memory_db=mem_db_path):
        record_memory_hits(["alpha", "beta"])

    con = sqlite3.connect(str(mem_db_path))
    rows = con.execute("SELECT name, hit_count FROM memories").fetchall()
    con.close()
    assert dict(rows) == {"alpha": 1, "beta": 1}


def test_record_memory_hits_noop_on_empty_list():
    """Must not raise or open a connection when there's nothing to record."""
    record_memory_hits([])  # no patch — would fail if it tried to open the real memory_db


def test_record_memory_hits_never_raises_on_db_error(tmp_path):
    """Best-effort instrumentation: a bad db path degrades silently (logged,
    not raised) so it can never break the prompt-injection path it's wired
    into from load_memories.py."""
    bad_path = tmp_path / "does" / "not" / "exist" / "MEMORY.sqlite"
    with patch("src.config.config", memory_db=bad_path):
        record_memory_hits(["alpha"])  # should not raise


# ---------------------------------------------------------------------------
# score_repo_memories / score_memories(cwd=...) merge
# ---------------------------------------------------------------------------

def _write_repo_memory(tmp_path, name, tags="", body="", mem_type="project"):
    import json

    store_dir = tmp_path / "repo_memory"
    store_dir.mkdir(exist_ok=True)
    store_path = store_dir / "memories.json"
    data = json.loads(store_path.read_text()) if store_path.exists() else {"meta": {}, "memories": {}}
    data["memories"][name] = {
        "name": name, "type": mem_type, "tags": tags, "body": body,
        "files": "", "related": "", "last_validated": "", "created_at": "",
    }
    store_path.write_text(json.dumps(data))
    return tmp_path


def test_score_repo_memories_returns_empty_when_no_store(tmp_path):
    assert score_repo_memories({"gate"}, str(tmp_path), _CFG) == {}


def test_score_repo_memories_returns_empty_when_no_cwd():
    assert score_repo_memories({"gate"}, "", _CFG) == {}


def test_score_repo_memories_scores_by_tag_overlap(tmp_path):
    _write_repo_memory(tmp_path, "repo-gate-fact", tags="gate framework", body="")
    results = score_repo_memories({"gate", "framework"}, str(tmp_path), _CFG)
    assert "repo-gate-fact" in results
    score, mem = results["repo-gate-fact"]
    assert score > 0
    assert mem["domain"] == "repo"
    assert mem["name"] == "repo-gate-fact"


def test_score_memories_merges_repo_candidates(mem_conn, tmp_path):
    """A repo_memory entry with no sqlite counterpart still surfaces via cwd=."""
    _insert(mem_conn, "sqlite-gate-fact", domain="claude-hooks", tags="gate", body="")
    _write_repo_memory(tmp_path, "repo-only-gate-fact", tags="gate", body="")

    results = score_memories({"gate"}, "claude-hooks", mem_conn, top_n=5, cwd=str(tmp_path))
    names = {m["name"] for m in results}
    assert "sqlite-gate-fact" in names
    assert "repo-only-gate-fact" in names


def test_score_memories_without_cwd_ignores_repo_store(mem_conn, tmp_path):
    """cwd="" (default) must not touch repo_memory at all — existing callers unaffected."""
    _insert(mem_conn, "sqlite-gate-fact", domain="claude-hooks", tags="gate", body="")
    _write_repo_memory(tmp_path, "repo-only-gate-fact", tags="gate", body="")

    results = score_memories({"gate"}, "claude-hooks", mem_conn, top_n=5)
    names = {m["name"] for m in results}
    assert "sqlite-gate-fact" in names
    assert "repo-only-gate-fact" not in names
