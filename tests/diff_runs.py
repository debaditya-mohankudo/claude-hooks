"""diff_runs — compare the last two pytest run_ids in test_logs.db.

Usage:
    uv run python tests/diff_runs.py
    uv run python tests/diff_runs.py --db tests/test_logs.db --runs 2
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

_DEFAULT_DB = Path(__file__).parent / "test_logs.db"


def _get_run_ids(conn: sqlite3.Connection, n: int = 2) -> list[str]:
    rows = conn.execute(
        "SELECT run_id FROM test_runs ORDER BY run_id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in rows]


def _test_outcomes(conn: sqlite3.Connection, run_id: str) -> dict[str, str]:
    """Return {nodeid: outcome} for a run_id.

    Outcomes come from test_runs-adjacent data: we read TEST_START sentinels
    scoped to this run_id from hook_logs, then cross-reference the test_runs
    summary for pass/fail counts. For per-test outcomes we use hook_logs
    ERROR rows as a fallback heuristic only when test_runs lacks per-test data.

    When pytest_runtest_logreport wrote per-test rows (future: test_outcomes table),
    this function will read from there instead.
    """
    # Per-test outcomes: infer from presence of ERROR rows within each test scope,
    # excluding known injection-test patterns.
    rows = conn.execute("""
        WITH scoped AS (
          SELECT
            h.id, h.level, h.message,
            (SELECT m.message FROM hook_logs m
             WHERE m.logger = 'pytest.marker'
               AND m.message LIKE '[TEST_START]%'
               AND m.run_id = ?
               AND m.id <= h.id
             ORDER BY m.id DESC LIMIT 1) AS test
          FROM hook_logs h
          WHERE h.run_id = ? AND h.logger != 'pytest.marker'
        )
        SELECT
          test,
          MAX(CASE WHEN level = 'ERROR'
                    AND test NOT LIKE '%test_fanout_failure_injection%'
                    AND test NOT LIKE '%test_query_tvim_error_returns_empty%'
                    AND test NOT LIKE '%test_handle_neighbors_error_returns_empty%'
               THEN 1 ELSE 0 END) AS has_unexpected_error
        FROM scoped
        WHERE test IS NOT NULL
        GROUP BY test
    """, (run_id, run_id)).fetchall()

    return {r[0]: ("failed" if r[1] else "passed") for r in rows}


def diff_runs(db_path: Path = _DEFAULT_DB, n: int = 2) -> dict:
    """Compare the last n run_ids. Returns a structured diff dict."""
    with sqlite3.connect(str(db_path)) as conn:
        run_ids = _get_run_ids(conn, n)

    if len(run_ids) < 2:
        return {"error": f"Need at least 2 runs, found {len(run_ids)}"}

    new_run, old_run = run_ids[0], run_ids[1]

    with sqlite3.connect(str(db_path)) as conn:
        old = _test_outcomes(conn, old_run)
        new = _test_outcomes(conn, new_run)

        old_meta = conn.execute(
            "SELECT n_tests, n_passed, n_failed FROM test_runs WHERE run_id = ?", (old_run,)
        ).fetchone()
        new_meta = conn.execute(
            "SELECT n_tests, n_passed, n_failed FROM test_runs WHERE run_id = ?", (new_run,)
        ).fetchone()

    all_tests = set(old) | set(new)

    regressions = sorted(t for t in all_tests if old.get(t) == "passed" and new.get(t) == "failed")
    fixes       = sorted(t for t in all_tests if old.get(t) == "failed" and new.get(t) == "passed")
    new_tests   = sorted(t for t in all_tests if t not in old)
    dropped     = sorted(t for t in all_tests if t not in new)

    return {
        "old_run":     old_run,
        "new_run":     new_run,
        "old_meta":    dict(zip(["n_tests", "n_passed", "n_failed"], old_meta)) if old_meta else {},
        "new_meta":    dict(zip(["n_tests", "n_passed", "n_failed"], new_meta)) if new_meta else {},
        "regressions": regressions,
        "fixes":       fixes,
        "new_tests":   new_tests,
        "dropped":     dropped,
    }


def _print_diff(d: dict) -> None:
    if "error" in d:
        print(f"Error: {d['error']}")
        return

    om, nm = d["old_meta"], d["new_meta"]
    print(f"\nDiff: {d['old_run']} → {d['new_run']}")
    print(f"  Old: {om.get('n_tests',0)} tests, {om.get('n_passed',0)} passed, {om.get('n_failed',0)} failed")
    print(f"  New: {nm.get('n_tests',0)} tests, {nm.get('n_passed',0)} passed, {nm.get('n_failed',0)} failed")

    def _section(label, items, marker):
        if items:
            print(f"\n{label} ({len(items)}):")
            for t in items:
                # shorten nodeid for readability
                short = t.replace("[TEST_START] ", "").split("::")[-1] if "::" in t else t
                print(f"  {marker} {short}")

    _section("Regressions (pass→fail)", d["regressions"], "✗")
    _section("Fixes       (fail→pass)", d["fixes"],       "✓")
    _section("New tests",               d["new_tests"],   "+")
    _section("Dropped tests",           d["dropped"],     "-")

    if not any([d["regressions"], d["fixes"], d["new_tests"], d["dropped"]]):
        print("\n  ✓ No changes between runs")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diff last two pytest run_ids in test_logs.db")
    parser.add_argument("--db",   default=str(_DEFAULT_DB), help="Path to test_logs.db")
    parser.add_argument("--runs", type=int, default=2,      help="Number of recent runs to compare")
    args = parser.parse_args()
    _print_diff(diff_runs(Path(args.db), args.runs))
