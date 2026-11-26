"""Move taskfw loop lessons from tasks.db into MEMORY.sqlite and close the old tables (task:59d8a962).

    uv run python scripts/migrate_loop_memory.py            # dry run: report, then roll back
    uv run python scripts/migrate_loop_memory.py --apply    # back up both DBs, copy, cut over

A slug that already names a general memory keeps the general row; the lesson's task links
and hit counts move onto it. Superseded lessons become `supersedes` links.

The copy runs under a write lock on tasks.db, and the same transaction installs triggers
that abort any later write to the old tables. A taskfw process still pointed at them
therefore fails loudly instead of recording lessons nobody will read.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from encdb import open_db  # noqa: E402

MEMORY_DB = Path.home() / ".claude" / "MEMORY.sqlite"
TASK_DB = Path.home() / ".taskfw" / "tasks.db"
MOVED = "loop memory moved to MEMORY.sqlite (task:59d8a962); set TASKFW_MEMORY_DB and restart taskfw"


class Refused(RuntimeError):
    pass


def normalise_scope(scope: str) -> str:
    """One spelling per repo: a path: scope with a git remote becomes git:, git: is lowercased."""
    if scope.startswith("path:"):
        r = subprocess.run(["git", "-C", scope[5:], "remote", "get-url", "origin"],
                           capture_output=True, text=True)
        url = r.stdout.strip()
        if r.returncode or not url:
            return scope
        url = url.split("://", 1)[-1].split("@", 1)[-1].replace(":", "/", 1)
        scope = "git:" + url.removesuffix("/").removesuffix(".git")
    return scope.lower() if scope.startswith("git:") else scope


def _lesson_scopes(tasks) -> dict[str, str]:
    """The one scope of the tasks a lesson was learned from; '' (global) for none or several."""
    rows = tasks.execute(
        "SELECT l.slug, t.scope FROM memory_links l JOIN tasks t ON t.id = l.task_id "
        "WHERE l.relation = 'learned_from'").fetchall()
    cache = {s: normalise_scope(s) for s in {r[1] for r in rows} if s}
    # A checkout that no longer exists has no remote to ask; a git: scope with its name stands in.
    by_name = {s.rsplit("/", 1)[-1]: s for s in cache.values() if s.startswith("git:")}
    for raw, scope in cache.items():
        if scope.startswith("path:") and not Path(scope[5:]).exists():
            cache[raw] = by_name.get(Path(scope[5:]).name.lower(), scope)
    found: dict[str, set[str]] = {}
    for slug, scope in rows:
        if scope:
            found.setdefault(slug, set()).add(cache[scope])
    return {slug: next(iter(s)) for slug, s in found.items() if len(s) == 1}


def _move(tasks, mem) -> dict:
    if mem.execute("SELECT 1 FROM memories WHERE origin = 'loop' LIMIT 1").fetchone():
        raise Refused("MEMORY.sqlite already holds loop rows; restore the backup before rerunning")
    lessons = tasks.execute(
        "SELECT slug, kind, text, superseded_by, created_at, updated_at, hit_count, last_hit "
        "FROM memories ORDER BY created_at, slug").fetchall()
    links = tasks.execute("SELECT slug, task_id, relation, created_at FROM memory_links").fetchall()
    scopes = _lesson_scopes(tasks)
    general = {r[0] for r in mem.execute("SELECT name FROM memories").fetchall()}
    shared = sorted(s[0] for s in lessons if s[0] in general)
    hits_before = mem.execute("SELECT COALESCE(SUM(hit_count), 0) FROM memories").fetchone()[0]
    rows_before = len(general)

    for slug, kind, text, _by, created, updated, hits, last_hit in lessons:
        if slug in general:
            mem.execute(
                "UPDATE memories SET hit_count = COALESCE(hit_count, 0) + ?, "
                "last_hit = NULLIF(MAX(COALESCE(last_hit, ''), COALESCE(?, '')), '') WHERE name = ?",
                (hits, last_hit, slug))
        else:
            # Every timestamp goes in on INSERT, so nothing needs a later UPDATE.
            mem.execute(
                "INSERT INTO memories (name, type, tags, body, origin, kind, scope, "
                "updated, created, hit_count, last_hit) "
                "VALUES (?, 'feedback', '', ?, 'loop', ?, ?, ?, ?, ?, ?)",
                (slug, text, kind, scopes.get(slug, ""), updated, created, hits, last_hit))
    ids = dict(mem.execute("SELECT name, id FROM memories").fetchall())
    for slug, task_id, relation, created in links:
        mem.execute(
            "INSERT INTO memory_task_links (memory_id, task_id, relation, created) VALUES (?, ?, ?, ?)",
            (ids[slug], task_id, relation, created))
    superseded = [(s[0], s[3], s[5]) for s in lessons if s[3]]
    for slug, by, updated in superseded:
        mem.execute(
            "INSERT INTO memory_links (src_id, dst_id, type, created) VALUES (?, ?, 'supersedes', ?)",
            (ids[by], ids[slug], updated))

    def count(sql: str):
        return mem.execute(sql).fetchone()[0]

    # (expected, found)
    checks = {
        "rows": (rows_before + len(lessons) - len(shared), count("SELECT COUNT(*) FROM memories")),
        "loop_rows": (len(lessons) - len(shared),
                      count("SELECT COUNT(*) FROM memories WHERE origin = 'loop'")),
        "hit_count": (hits_before + sum(s[6] for s in lessons),
                      count("SELECT COALESCE(SUM(hit_count), 0) FROM memories")),
        "task_links": (dict(Counter(link[2] for link in links)),
                       dict(mem.execute("SELECT relation, COUNT(*) FROM memory_task_links "
                                        "GROUP BY relation").fetchall())),
        "superseded": (len(superseded), count(
            "SELECT COUNT(*) FROM memory_links l JOIN memories m ON m.id = l.dst_id "
            "WHERE l.type = 'supersedes' AND m.origin = 'loop'")),
    }
    bad = {k: v for k, v in checks.items() if v[0] != v[1]}
    if bad:
        raise Refused(f"counts do not reconcile (expected, found): {bad}")
    return {
        "lessons": len(lessons),
        "merged_into_general": shared,
        "scopes": dict(Counter(scopes.get(s[0], "") or "(global)" for s in lessons if s[0] not in general)),
        "reconciled": {k: v[1] for k, v in checks.items()},
    }


def _backup(path: Path, stamp: str) -> Path:
    """An encrypted copy beside the source; VACUUM INTO reads through any WAL."""
    out = path.with_name(f"{path.name}.pre-59d8a962.{stamp}")
    con = open_db(path)
    try:
        con.execute("VACUUM INTO '{}'".format(str(out).replace("'", "''")))
    finally:
        con.close()
    check = open_db(out, readonly=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise Refused(f"backup {out} failed its integrity check")
    finally:
        check.close()
    return out


def migrate(task_db: Path, memory_db: Path, apply: bool = False) -> dict:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backups = [str(_backup(p, stamp)) for p in (task_db, memory_db)] if apply else []
    tasks, mem = open_db(task_db), open_db(memory_db)
    try:
        for con in (tasks, mem):
            con.execute("PRAGMA busy_timeout = 5000")
        tasks.execute("BEGIN IMMEDIATE")
        mem.execute("BEGIN IMMEDIATE")
        report = _move(tasks, mem)
        if apply:
            for table in ("memories", "memory_links"):
                for op in ("INSERT", "UPDATE", "DELETE"):
                    tasks.execute(
                        f"CREATE TRIGGER {table}_moved_{op.lower()} BEFORE {op} ON {table} "
                        f"BEGIN SELECT RAISE(ABORT, '{MOVED}'); END")
            mem.commit()
            tasks.commit()
        else:
            mem.rollback()
            tasks.rollback()
    except BaseException:
        mem.rollback()
        tasks.rollback()
        raise
    finally:
        mem.close()
        tasks.close()
    return {"applied": apply, "backups": backups, **report}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task-db", type=Path, default=TASK_DB)
    ap.add_argument("--memory-db", type=Path, default=MEMORY_DB)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    print(json.dumps(migrate(a.task_db, a.memory_db, a.apply), indent=1))
