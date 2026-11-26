"""Tests for langchain_learning/nodes/_memory_scoring.py — combination signal + related boost."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from langchain_learning.nodes._memory_scoring import (
    record_memory_hits,
    score_memories,
)
from src.db.schema import MEMORIES_DDL

_CFG = {
    "top_n": 5,
    "tag_weight": 3.0,
    "body_weight": 1.0,
    "recency_boost": 1.2,
    "recency_penalty": 0.8,
    "recency_boost_days": 30,
    "recency_penalty_days": 180,
    "min_keyword_score": 0.0,
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


def _insert(con, name, tags="", body="", related=""):
    con.execute(
        "INSERT INTO memories (name, type, tags, body, related) VALUES (?,?,?,?,?)",
        (name, "project", tags, body, related),
    )
    con.commit()


def test_old_memory_beyond_500_newer_rows_is_scored(mem_conn):
    """There is no candidate window: age affects the score, never eligibility."""
    _insert(mem_conn, "old-one", tags="widget")
    mem_conn.execute("UPDATE memories SET updated='2020-01-01 00:00:00' WHERE name='old-one'")
    for i in range(501):
        _insert(mem_conn, f"filler-{i}", tags="zzz")
    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=_CFG):
        assert [m["name"] for m in score_memories({"widget"}, mem_conn)] == ["old-one"]


def test_high_scorer_boosts_related_neighbour(mem_conn):
    """A memory with no keyword match should appear if linked via related to a high scorer."""
    _insert(mem_conn, "gate-framework", tags="gate framework prereq", body="Gate ABC pattern", related="gate-prereq-tracking")
    _insert(mem_conn, "gate-prereq-tracking", tags="obscure-unrelated-zzz", body="obscure-unrelated-zzz")

    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=_CFG):
        results = score_memories({"gate", "framework"}, mem_conn, top_n=5)

    names = [m["name"] for m in results]
    assert "gate-framework" in names, "seed should be in results"
    assert "gate-prereq-tracking" in names, "related neighbour should be boosted into results"


def test_neighbour_boost_is_additive(mem_conn):
    """A neighbour that already scores on its own should score higher after boost."""
    _insert(mem_conn, "seed", tags="gate", body="gate framework", related="neighbour")
    _insert(mem_conn, "neighbour", tags="gate", body="gate prereq")

    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=_CFG):
        results_with_related = score_memories({"gate"}, mem_conn, top_n=5)

    # Remove related from seed to get baseline neighbour score
    mem_conn.execute("UPDATE memories SET related='' WHERE name='seed'")
    mem_conn.commit()

    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=_CFG):
        results_without_related = score_memories({"gate"}, mem_conn, top_n=5)

    def get_rank(results, name):
        names = [m["name"] for m in results]
        return names.index(name) if name in names else 999

    # neighbour should rank higher (lower index) when boost is applied
    assert get_rank(results_with_related, "neighbour") <= get_rank(results_without_related, "neighbour")


def test_max_neighbours_per_seed_respected(mem_conn):
    """Only related_max_neighbours (2) neighbours per seed should be boosted.

    Noise memories have no tag/body overlap, so they only appear via boost.
    """
    _insert(mem_conn, "seed", tags="gate", body="gate", related="n1,n2,n3")
    _insert(mem_conn, "n1", tags="zzz", body="zzz")
    _insert(mem_conn, "n2", tags="zzz", body="zzz")
    _insert(mem_conn, "n3", tags="zzz", body="zzz")

    cfg = {**_CFG, "related_max_neighbours": 2, "top_n": 10}
    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=cfg):
        results = score_memories({"gate"}, mem_conn, top_n=10)

    names = [m["name"] for m in results]
    boosted = [n for n in ["n1", "n2", "n3"] if n in names]
    assert len(boosted) == 2, f"expected 2 boosted neighbours, got {boosted}"


def test_zero_boost_factor_disables_graph(mem_conn):
    """related_boost_factor=0 should not surface zero-direct-scoring neighbours.

    Neighbour has no tag/body overlap, so it only appears via boost.
    """
    _insert(mem_conn, "seed", tags="gate", body="gate", related="silent-neighbour")
    _insert(mem_conn, "silent-neighbour", tags="zzz", body="zzz")

    cfg = {**_CFG, "related_boost_factor": 0.0}
    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=cfg):
        results = score_memories({"gate"}, mem_conn, top_n=5)

    names = [m["name"] for m in results]
    assert "silent-neighbour" not in names


# ---------------------------------------------------------------------------
# memory_links neighbour boost (task:5530af93)
# ---------------------------------------------------------------------------

@pytest.fixture
def link_conn(mem_conn):
    from src.db.schema import migrate_memory_db
    migrate_memory_db(mem_conn)
    return mem_conn


def _id(con, name):
    return con.execute("SELECT id FROM memories WHERE name=?", (name,)).fetchone()[0]


def _link(con, a, b, etype="relates_to"):
    x, y = _id(con, a), _id(con, b)
    if etype == "relates_to":
        x, y = min(x, y), max(x, y)
    con.execute("INSERT INTO memory_links (src_id, dst_id, type) VALUES (?,?,?)", (x, y, etype))
    con.commit()


def _run(con, tokens, **over):
    cfg = {**_CFG, "top_n": 10, **over}
    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=cfg):
        return score_memories(tokens, con, top_n=10)


def test_link_neighbour_with_no_term_match_surfaces(link_conn):
    _insert(link_conn, "old-neighbour", tags="zzz", body="zzz")
    link_conn.execute("UPDATE memories SET updated='2020-01-01 00:00:00' WHERE name='old-neighbour'")
    _insert(link_conn, "seed", tags="gate", body="gate")
    _link(link_conn, "seed", "old-neighbour")
    names = [m["name"] for m in _run(link_conn, {"gate"})]
    assert "old-neighbour" in names


def test_link_neighbours_chosen_by_prompt_match_not_order(link_conn):
    _insert(link_conn, "seed", tags="gate", body="gate")
    for n in ("a1", "a2", "a3"):
        _insert(link_conn, n, tags="zzz", body="zzz")
        _link(link_conn, "seed", n)
    _insert(link_conn, "matching", tags="gate door", body="gate door")
    _link(link_conn, "seed", "matching")
    names = [m["name"] for m in _run(link_conn, {"gate"}, related_max_neighbours=1)]
    assert "matching" in names
    assert sum(n in names for n in ("a1", "a2", "a3")) == 0


def test_hub_target_boost_is_capped(link_conn):
    _insert(link_conn, "hub", tags="zzz", body="zzz")
    for i in range(5):
        _insert(link_conn, f"seed{i}", tags="gate", body="gate")
        _link(link_conn, f"seed{i}", "hub")
    order = [m["name"] for m in _run(link_conn, {"gate"}, related_boost_factor=0.5, related_target_cap=0.5)]
    assert "hub" in order
    assert order.index("hub") > order.index("seed0")  # capped below every direct seed


def test_superseded_memory_demoted_below_superseder(link_conn):
    _insert(link_conn, "old-way", tags="gate", body="gate")
    _insert(link_conn, "new-way", tags="gate", body="gate")
    link_conn.execute("UPDATE memories SET updated='2020-01-01 00:00:00' WHERE name='new-way'")  # new-way would otherwise rank lower
    _link(link_conn, "new-way", "old-way", "supersedes")
    names = [m["name"] for m in _run(link_conn, {"gate"})]
    assert names.index("new-way") < names.index("old-way")


def test_superseding_seed_does_not_pull_in_old_memory(link_conn):
    _insert(link_conn, "new-way", tags="gate", body="gate")
    _insert(link_conn, "old-way", tags="zzz", body="zzz")
    _link(link_conn, "new-way", "old-way", "supersedes")
    assert "old-way" not in [m["name"] for m in _run(link_conn, {"gate"})]


def test_old_seed_pulls_in_its_superseder(link_conn):
    _insert(link_conn, "old-way", tags="gate", body="gate")
    _insert(link_conn, "new-way", tags="zzz", body="zzz")
    _link(link_conn, "new-way", "old-way", "supersedes")
    assert "new-way" in [m["name"] for m in _run(link_conn, {"gate"})]


def test_contradicting_pair_is_marked_and_not_boosted(link_conn):
    _insert(link_conn, "claim", tags="gate", body="gate")
    _insert(link_conn, "counter", tags="gate", body="gate")
    _insert(link_conn, "lonely-foe", tags="zzz", body="zzz")
    _link(link_conn, "claim", "counter", "contradicts")
    _link(link_conn, "claim", "lonely-foe", "contradicts")
    res = {m["name"]: m for m in _run(link_conn, {"gate"})}
    assert res["claim"]["conflicts_with"] == ["counter"]
    assert res["counter"]["conflicts_with"] == ["claim"]
    assert "lonely-foe" not in res


def test_related_source_related_keeps_legacy_behaviour(link_conn):
    _insert(link_conn, "seed", tags="gate", body="gate", related="col-neighbour")
    _insert(link_conn, "col-neighbour", tags="zzz", body="zzz")
    _insert(link_conn, "link-neighbour", tags="zzz", body="zzz")
    _link(link_conn, "seed", "link-neighbour")
    names = [m["name"] for m in _run(link_conn, {"gate"}, related_source="related")]
    assert "col-neighbour" in names and "link-neighbour" not in names


def test_missing_memory_links_table_falls_back_to_related_column(mem_conn):
    _insert(mem_conn, "seed", tags="gate", body="gate", related="col-neighbour")
    _insert(mem_conn, "col-neighbour", tags="zzz", body="zzz")
    assert "col-neighbour" in [m["name"] for m in _run(mem_conn, {"gate"})]


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
    con.execute("INSERT INTO memories (name, type) VALUES ('alpha', 'project')")
    con.execute("INSERT INTO memories (name, type) VALUES ('beta', 'project')")
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
# BM25 over tags (task:c48b4190)
# ---------------------------------------------------------------------------

def _names(con, tokens, n=5):
    return [m["name"] for m in score_memories(tokens, con, top_n=n)]


def test_rare_tag_outranks_common_tag(mem_conn):
    """IDF: matching a term few memories carry beats matching one most carry."""
    for i in range(6):
        _insert(mem_conn, f"filler-{i}", tags="stocks, tracker, notes")
    _insert(mem_conn, "rare", tags="sqlcipher, notes")
    _insert(mem_conn, "common", tags="tracker, notes")
    assert _names(mem_conn, {"sqlcipher", "tracker"})[0] == "rare"


def test_many_tags_not_penalised_vs_few(mem_conn):
    """A well-tagged memory must not lose to a sparsely tagged one for the same match."""
    _insert(mem_conn, "sparse", tags="gate")
    _insert(mem_conn, "rich", tags="gate, commit, hook, memory, retrieval, scoring, tuning")
    for i in range(4):
        _insert(mem_conn, f"other-{i}", tags="unrelated, topic, words")
    top = _names(mem_conn, {"gate"}, n=2)
    assert set(top) == {"sparse", "rich"}
    scores = {m["name"]: m for m in score_memories({"gate"}, mem_conn, top_n=2)}
    assert scores["rich"] and scores["sparse"]


def test_no_tag_match_scores_nothing(mem_conn):
    _insert(mem_conn, "a", tags="alpha, beta")
    _insert(mem_conn, "b", tags="gamma, delta")
    assert _names(mem_conn, {"zzzz"}) == []


def test_index_rebuilds_only_when_tags_change(mem_conn):
    from langchain_learning.nodes import _memory_scoring as ms
    _insert(mem_conn, "a", tags="alpha, beta")
    _insert(mem_conn, "b", tags="gamma, delta")
    ms._bm25_cache.update(sig=None)
    score_memories({"alpha"}, mem_conn, top_n=3)
    first = ms._bm25_cache["bm25"]
    score_memories({"gamma"}, mem_conn, top_n=3)
    assert ms._bm25_cache["bm25"] is first          # unchanged table -> cached index reused
    mem_conn.execute("UPDATE memories SET tags='alpha, epsilon' WHERE name='b'")
    mem_conn.commit()
    assert _names(mem_conn, {"epsilon"}) == ["b"]   # tag edit -> index rebuilt and searchable
    assert ms._bm25_cache["bm25"] is not first


# --- match filter: 2 distinct terms, or 1 rare term ---------------------------

def _filter_corpus(con, n_common=10):
    """`check` is common (n_common memories); `harness` appears in one."""
    for i in range(n_common):
        _insert(con, f"common-{i}", tags=f"check topic{i}")
    _insert(con, "replay-harness", tags="harness replay")
    _insert(con, "check-and-replay", tags="check replay")


def test_lone_common_word_does_not_match(mem_conn):
    _filter_corpus(mem_conn)
    names = {m["name"] for m in score_memories({"check", "zzzunrelated"}, mem_conn, top_n=20)}
    assert not names & {f"common-{i}" for i in range(10)}


def test_two_distinct_terms_match_even_if_common(mem_conn):
    _filter_corpus(mem_conn)
    names = {m["name"] for m in score_memories({"check", "replay"}, mem_conn, top_n=20)}
    assert "check-and-replay" in names


def test_single_rare_term_matches(mem_conn):
    _filter_corpus(mem_conn)
    names = {m["name"] for m in score_memories({"harness", "zzzunrelated"}, mem_conn, top_n=20)}
    assert "replay-harness" in names


def test_one_word_prompt_needs_only_one_hit(mem_conn):
    _filter_corpus(mem_conn)
    names = {m["name"] for m in score_memories({"check"}, mem_conn, top_n=20)}
    assert "common-0" in names


def test_rare_term_df_is_configurable(mem_conn):
    _filter_corpus(mem_conn)
    cfg = {**_CFG, "rare_term_df": 20}
    with patch("langchain_learning.nodes._memory_scoring.load_scoring_cfg", return_value=cfg):
        names = {m["name"] for m in score_memories({"check", "zzzunrelated"}, mem_conn, top_n=20)}
    assert "common-0" in names


def test_trace_lists_candidates_with_matched_terms(mem_conn):
    _insert(mem_conn, "replay-harness", tags="harness replay")
    _insert(mem_conn, "other-mem", tags="unrelated")
    trace: list = []
    score_memories({"harness", "replay"}, mem_conn, top_n=3, trace=trace)
    assert trace[0]["name"] == "replay-harness"
    assert trace[0]["hits"] == ["harness", "replay"] and trace[0]["score"] > 0
