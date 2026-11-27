"""Replay harness — which memories does the current code return for real past prompts?

Usage (from the repo root):
    uv run python tests/replay_harness.py --capture [--since YYYY-MM-DD] [--limit 100]
    uv run python tests/replay_harness.py --replay

Capture takes prompts from memory_audit.sqlite, snapshots MEMORY.sqlite next to the
baseline, and records the memory slugs the dispatcher's UPS turn returns for each.
Replay reruns the same prompts against that snapshot, with the recency clock fixed
at capture time, so a deviation means the code (or scoring config) changed, not the
memory store. Neither writes to a live store, so replayed turns are not logged.
Capture from known-good code.

Hook-log parsing and synthetic prompts were removed: the log keeps only a prompt's
length, and replaying filler text said nothing about ranking.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "src", _ROOT / "hooks"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hooks.dispatcher as dispatcher  # noqa: E402  (sys.path set up above)
import hooks.server_memory  # noqa: E402
from hooks import session_store  # noqa: E402
from langchain_learning.nodes import _memory_audit, _memory_scoring, _skill_memories  # noqa: E402
from langchain_learning.nodes.load_memories import LoadMemoriesNode  # noqa: E402
from langchain_learning.retrievers import CombinationSignalRetriever  # noqa: E402
from src.config import config  # noqa: E402
from src.encdb import open_db  # noqa: E402

BASELINE = Path(__file__).parent / "replay_baseline.json"
SNAPSHOT = Path(__file__).parent / "replay_baseline_memory.sqlite"


def load_prompts(since: str | None, limit: int) -> list[dict]:
    """The latest `limit` audited prompts, oldest first (area carry-over depends on order)."""
    since_ts = datetime.fromisoformat(since).timestamp() if since else 0
    test_prefixes = (session_store.TEST_SESSION_PREFIX, *hooks.server_memory._TEST_PREFIXES)
    not_test = " AND session_id NOT LIKE ?" * len(test_prefixes)
    conn = open_db(_memory_audit.AUDIT_DB, readonly=True)
    try:
        rows = conn.execute(
            f"SELECT id, session_id, prompt FROM memory_audit WHERE ts >= ?{not_test} ORDER BY id DESC LIMIT ?",
            (since_ts, *(p + "%" for p in test_prefixes), limit),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": i, "session": s, "prompt": p} for i, s, p in reversed(rows)]


def snapshot_memory_db(dest: Path, source: Path | None = None) -> None:
    """Consistent copy of MEMORY.sqlite; VACUUM INTO keeps the source's encryption."""
    dest.unlink(missing_ok=True)
    conn = open_db(source or config.memory_db)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()


@contextmanager
def _isolated(snapshot: Path, as_of: datetime):
    """Point the UPS turn at the snapshot, fix its clock and drop every write it makes."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return as_of.astimezone(tz)

    node = LoadMemoriesNode(CombinationSignalRetriever(snapshot))
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)  # the hook log is a live store too
    try:
        with mock.patch.object(dispatcher, "_load_memories_node", node), \
             mock.patch.object(_memory_scoring, "datetime", _Clock), \
             mock.patch.object(_memory_scoring, "record_memory_hits"), \
             mock.patch.object(_memory_audit, "record"), \
             mock.patch.object(_skill_memories, "note_prompt_injected"), \
             mock.patch.object(hooks.server_memory, "record_memories"):
            yield
    finally:
        logging.disable(previous)


def replay(prompts: list[dict], snapshot: Path, as_of: datetime) -> list[dict]:
    """Run each prompt through the dispatcher's UPS turn; add the returned slugs in rank order."""
    if not snapshot.exists():
        raise FileNotFoundError(f"memory snapshot {snapshot} is missing; run --capture")
    out, sessions = [], set()
    with _isolated(snapshot, as_of):
        try:
            for event in prompts:
                sid = f"{session_store.TEST_SESSION_PREFIX}replay-{event['session']}"
                sessions.add(sid)
                result = dispatcher._run_user_prompt_turn(event["prompt"], sid, str(_ROOT))
                out.append({**event, "memories": [m["name"] for m in result["memories"]]})
        finally:
            for sid in sessions:
                session_store.end(sid)
    return out


def diff_events(baseline: list[dict], replayed: list[dict]) -> list[dict]:
    """Events whose returned slugs, or their order, differ."""
    return [
        {"id": b["id"], "prompt": b["prompt"], "baseline": b["memories"], "replayed": r["memories"]}
        for b, r in zip(baseline, replayed, strict=True)
        if b["memories"] != r["memories"]
    ]


def format_deviation(d: dict) -> str:
    return (f"  audit id={d['id']} prompt={d['prompt'][:70]!r}\n"
            f"      baseline: {d['baseline']}\n"
            f"      replayed: {d['replayed']}")


def cmd_capture(args) -> None:
    prompts = load_prompts(args.since, args.limit)
    snapshot_memory_db(SNAPSHOT)
    as_of = datetime.now(timezone.utc)
    events = replay(prompts, SNAPSHOT, as_of)
    hits = sum(1 for e in events if e["memories"])
    if not hits:
        # The retriever fails open, so an unreadable DB would otherwise become an all-empty baseline.
        sys.exit(f"No memories returned for any of {len(events)} prompts; baseline not saved.")
    BASELINE.write_text(json.dumps({"as_of": as_of.isoformat(), "events": events}, indent=2))
    print(f"Baseline saved → {BASELINE}: {len(events)} prompts, {hits} with memories")


def cmd_replay(baseline: dict) -> list[dict]:
    events = baseline["events"]
    replayed = replay(events, SNAPSHOT, datetime.fromisoformat(baseline["as_of"]))
    deviations = diff_events(events, replayed)
    print(f"Replayed {len(replayed)} prompts, deviations: {len(deviations)}")
    for d in deviations:
        print(format_deviation(d))
    return deviations


def main():
    parser = argparse.ArgumentParser(description="Replay harness for claude-hooks")
    parser.add_argument("--capture", action="store_true", help="Snapshot memories and capture a baseline")
    parser.add_argument("--replay",  action="store_true", help="Replay and diff vs baseline")
    parser.add_argument("--since",   help="Only prompts from this date (YYYY-MM-DD)")
    parser.add_argument("--limit",   type=int, default=100, help="Max prompts")
    args = parser.parse_args()

    if not args.capture and not args.replay:
        parser.print_help()
        sys.exit(1)
    if args.capture:
        cmd_capture(args)
    if args.replay:
        if not BASELINE.exists():
            sys.exit(f"No baseline found at {BASELINE}. Run --capture first.")
        if cmd_replay(json.loads(BASELINE.read_text())):
            sys.exit(1)


if __name__ == "__main__":
    main()
