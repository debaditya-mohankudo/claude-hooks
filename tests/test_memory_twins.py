"""The cross-store twin detector (task:b0599ae7).

A lesson kept in both MEMORY.sqlite and taskfw loop memory can be superseded on
the taskfw side and keep being injected from MEMORY.sqlite, because
MEMORY.sqlite has no superseded_by column and LoadMemoriesNode has no notion of
standing. ``scripts/memory_twins.py`` is the check that catches that.

The synthetic cases build both stores as plain SQLite in tmp_path — src.encdb
opens an unencrypted file with the stdlib, so no key and no production write is
needed, which is why none of this touches ~/.claude/MEMORY.sqlite or
~/.taskfw/tasks.db. The one live test is a guard on the real stores and skips
when either is unreadable, because this check must never block a commit on a
machine that has no taskfw DB.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from tests.fixtures.db_factories import make_memory_db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memory_twins  # noqa: E402


def _loop_store(path: Path, rows: list[tuple[str, str]]) -> Path:
    """taskfw's loop-memory table, built inline on purpose.

    The MEMORY.sqlite side uses tests/fixtures/db_factories.make_memory_db so it
    tracks src/db/schema.py. This side has no such source: the schema belongs to
    taskfw (taskfw/db/schema.py:MEMORIES) and this repo deliberately owns no
    copy of it. Only the two columns the detector reads are modelled here.
    """
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memories (slug TEXT PRIMARY KEY, superseded_by TEXT NOT NULL DEFAULT '')")
    con.executemany("INSERT INTO memories VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return path


@pytest.fixture
def stores(tmp_path):
    """A twin (in both), a MEMORY-only slug and a loop-only slug. The twin is live."""
    mem = make_memory_db(tmp_path, [{"name": "shared-lesson"}, {"name": "memory-only"}])
    loop = _loop_store(tmp_path / "tasks.db",
                       [("shared-lesson", ""), ("loop-only", "")])
    return mem, loop


def _supersede(loop: Path, by: str = "shared-lesson-v2") -> None:
    """Retire the loop-memory copy of the twin, as task_memory__supersede does."""
    con = sqlite3.connect(loop)
    con.execute("UPDATE memories SET superseded_by = ? WHERE slug = 'shared-lesson'", (by,))
    con.commit()
    con.close()


@pytest.fixture
def wire_main(stores, monkeypatch):
    """Point main()'s module-level store paths at the fixture stores."""
    def _wire() -> None:
        monkeypatch.setattr(memory_twins, "MEMORY_DB", stores[0])
        monkeypatch.setattr(memory_twins, "TASKFW_DB", stores[1])
        monkeypatch.setattr(sys, "argv", ["memory_twins.py"])
    return _wire


def test_census_finds_only_the_shared_slug(stores):
    """A twin is a slug in BOTH stores — single-store slugs are not twins."""
    twins = memory_twins.census(*stores).twins
    assert [t.slug for t in twins] == ["shared-lesson"]
    # The factory leaves `updated` to the schema default, so assert it was read
    # rather than asserting a literal the fixture does not control.
    assert twins[0].memory_updated


def test_live_twin_is_not_stale(stores):
    assert memory_twins.census(*stores).twins[0].stale is False


def test_superseding_the_loop_side_makes_the_injected_twin_stale(stores):
    """The whole point: retiring the loop copy marks the still-injected copy stale.

    MEMORY.sqlite keeps serving ``shared-lesson`` to LoadMemoriesNode either
    way — nothing in the injection path reads standing — so this flag is the
    only thing standing between a retired lesson and a prompt that trusts it.
    """
    mem, loop = stores
    _supersede(loop)

    twin = memory_twins.census(mem, loop).twins[0]
    assert twin.stale is True
    assert twin.superseded_by == "shared-lesson-v2"


def test_stale_twin_fails_the_check(stores, wire_main, capsys):
    """main() exits non-zero and names the replacement, so the fix needs no digging."""
    _supersede(stores[1])
    wire_main()

    assert memory_twins.main() == 1
    assert "shared-lesson-v2" in capsys.readouterr().err


def test_clean_census_passes(stores, wire_main):
    wire_main()
    assert memory_twins.main() == 0


@pytest.mark.parametrize("missing", [0, 1])
def test_missing_store_fails_open(stores, tmp_path, missing, capsys):
    """No store, no census — and no traceback. A gate here must never block a commit."""
    paths = list(stores)
    paths[missing] = tmp_path / "absent.sqlite"
    result = memory_twins.census(*paths)
    assert result.twins == []
    assert result.missing == [str(paths[missing])]
    assert result.unreadable == []


def test_unreadable_store_is_reported_not_silently_empty(stores, tmp_path, capsys):
    """A store that is PRESENT but cannot be read must not look like a clean run.

    This is the distinction the live guard below turns on: no twins because
    there are none, versus no twins because the check could not see anything.
    Collapsing the two is how a guard quietly stops guarding.
    """
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database at all")

    result = memory_twins.census(stores[0], junk)
    assert result.twins == []
    assert result.missing == []
    assert result.unreadable == [str(junk)]
    assert "cannot read" in capsys.readouterr().err


def test_live_stores_have_no_stale_twin():
    """The real guard: nothing superseded in taskfw is still injected from MEMORY.sqlite."""
    result = memory_twins.census()
    if result.missing:
        pytest.skip(f"memory store(s) not on this machine: {result.missing}")
    # Not a skip. A store that is there but unreadable means this check is not
    # checking, and a silent skip at -q would hide exactly that.
    assert not result.unreadable, (
        f"memory store(s) present but unreadable: {result.unreadable}. The twin check "
        "cannot run — fix the decryption key or the schema before trusting a green suite."
    )
    stale = [(t.slug, t.superseded_by) for t in result.stale]
    assert not stale, (
        f"superseded in taskfw loop memory but still injected from MEMORY.sqlite: {stale}. "
        "Rewrite the MEMORY.sqlite copy to the replacement's content, or delete it. "
        "Run `uv run python scripts/memory_twins.py` for the full census."
    )
