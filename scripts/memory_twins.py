"""Cross-store twin detector: a slug that lives in BOTH memory stores (task:b0599ae7).

    uv run python scripts/memory_twins.py          # list the twins, exit 1 if any is stale
    uv run python scripts/memory_twins.py --json   # same census as JSON

Two stores hold lessons and only one of them is injected:

  * ``~/.claude/MEMORY.sqlite`` — scored and injected into every prompt by
    ``LoadMemoriesNode``. Its schema has no notion of a superseded row: a
    memory competes on keyword relevance until someone edits or deletes it.
  * ``~/.taskfw/tasks.db`` (taskfw loop memory) — read on request via
    ``task_memory__recall``, which *does* exclude superseded rows and derives
    a ``standing`` per row.

So a lesson kept in both places can be retired on the taskfw side and keep
being injected from MEMORY.sqlite, silently, with nothing to notice it.

**This is a detector, not a sync** (task:b0599ae7 decision, 2026-11-25). The
drift had not actually happened when this was written — 10 loop memories were
superseded and none of them had a MEMORY.sqlite twin — so what was missing was
enforcement of a hand process that had held, not a repair. The rejected
alternatives are recorded on the task: an injection-time filter in
``LoadMemoriesNode`` (a per-prompt dependency on a second DB, and taskfw
exposes no CLI to ask), and retiring the twins outright (lossy — loop memory is
never auto-injected).

Both stores are opened READ-ONLY through the vendored ``src/encdb`` opener,
which handles the SQLCipher encryption on each. Nothing here imports taskfw:
the only coupling is one query, ``SELECT slug, superseded_by FROM memories``,
against a table whose shape is pinned by taskfw/db/schema.py:MEMORIES.

Fails open. A missing store, a missing key, an unreadable file — each yields an
empty census and a note, never a traceback and never a blocked commit.
"""
from __future__ import annotations

import argparse
import json
from contextlib import closing
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import config  # noqa: E402
from src.encdb import open_db  # noqa: E402

# From config so CLAUDE_HOOKS_MEMORY_DB repoints this check with the rest of the
# pipeline. taskfw's DB has no such setting here and must not grow one: this
# repo owns no part of taskfw's configuration, so the path is a literal.
MEMORY_DB = config.memory_db
TASKFW_DB = Path.home() / ".taskfw" / "tasks.db"


@dataclass(frozen=True)
class Twin:
    """One slug present in both stores, with the loop side's retirement state."""

    slug: str
    superseded_by: str      # loop-memory side; "" means live
    memory_updated: str     # MEMORY.sqlite side, for deciding which copy is newer

    @property
    def stale(self) -> bool:
        """The loop side was retired, so the injected MEMORY.sqlite copy is obsolete."""
        return bool(self.superseded_by)


@dataclass(frozen=True)
class Census:
    """The twins, plus why a store contributed nothing.

    ``missing`` and ``unreadable`` are separate on purpose. Collapsing both into
    an empty result is how a check stops checking without anyone noticing: a
    machine with no taskfw DB and a machine whose decryption key has gone
    produce the same silence, and the second one is a broken guard reading as a
    clean run. A caller that must not be fooled — the pytest guard — skips on
    ``missing`` and fails on ``unreadable``.
    """

    twins: list[Twin]
    missing: list[str] = field(default_factory=list)       # store not on this machine
    unreadable: list[str] = field(default_factory=list)    # present, but the read failed

    @property
    def stale(self) -> list[Twin]:
        return [t for t in self.twins if t.stale]


def _slugs(path: Path, query: str) -> dict[str, str] | None:
    """``{first column: second column}`` from a read-only query, or None if unread.

    Fail open: a missing decryption key, a locked DB or a renamed column each
    yield None rather than an exception, because this runs in a check that must
    never block a commit. None is distinct from ``{}`` — an empty store was
    read successfully — so the caller can report what actually happened. The
    second column is normalised to "" so callers never see NULL.
    """
    try:
        with closing(open_db(str(path), readonly=True)) as con:
            return {r[0]: (r[1] or "") for r in con.execute(query)}
    except Exception as exc:          # EncDBError, OperationalError, …
        print(f"[memory_twins] cannot read {path}: {exc}", file=sys.stderr)
        return None


def census(memory_db: Path | None = None, taskfw_db: Path | None = None) -> Census:
    """Every slug held by both stores, sorted by slug.

    The paths default to the module constants and are resolved per call, not
    bound at definition time, so a caller (or a test) can repoint the stores by
    setting MEMORY_DB / TASKFW_DB and have main() follow.
    """
    paths = (MEMORY_DB if memory_db is None else memory_db,
             TASKFW_DB if taskfw_db is None else taskfw_db)
    queries = ("SELECT name, updated FROM memories",
               "SELECT slug, superseded_by FROM memories")

    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        return Census(twins=[], missing=missing)

    read = [_slugs(p, q) for p, q in zip(paths, queries)]
    unreadable = [str(p) for p, r in zip(paths, read) if r is None]
    if unreadable:
        return Census(twins=[], unreadable=unreadable)

    memories, loop = read
    twins = sorted(
        (Twin(slug=s, superseded_by=loop[s], memory_updated=memories[s])
         for s in memories.keys() & loop.keys()),
        key=lambda t: t.slug,
    )
    return Census(twins=twins)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="emit the census as JSON")
    args = ap.parse_args()

    c = census()
    if args.json:
        print(json.dumps({"twins": [asdict(t) | {"stale": t.stale} for t in c.twins],
                          "missing": c.missing, "unreadable": c.unreadable}, indent=2))
    else:
        for note, label in ((c.missing, "not on this machine"), (c.unreadable, "UNREADABLE")):
            for p in note:
                print(f"  store {label}: {p}")
        if not c.twins:
            print("No cross-store twins.")
        for t in c.twins:
            state = f"SUPERSEDED BY {t.superseded_by}" if t.stale else "live"
            print(f"  {'STALE' if t.stale else 'ok   '}  {t.slug}\n"
                  f"           loop: {state}   MEMORY.sqlite updated: {t.memory_updated}")
        if c.twins:
            print(f"\n{len(c.twins)} twin(s), {len(c.stale)} stale.")

    if c.stale:
        print("\nA superseded lesson is still being injected from MEMORY.sqlite. Fix with:",
              file=sys.stderr)
        for t in c.stale:
            print(f"  mac memory get {t.superseded_by}   # the replacement\n"
                  f"  mac memory add {t.slug} ...        # rewrite, or: mac memory delete {t.slug}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
