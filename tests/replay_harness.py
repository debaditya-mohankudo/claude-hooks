"""Replay harness — feed real claude_hooks.sqlite UPS events through the current graph.

Usage:
    # Capture a baseline snapshot from recent logs:
    uv run python tests/replay_harness.py --capture --since 2026-06-13 --limit 100

    # Replay against current graph and diff vs baseline:
    uv run python tests/replay_harness.py --replay

    # Both in one shot (capture fresh, then diff against saved baseline):
    uv run python tests/replay_harness.py --capture --replay --since 2026-06-14

Baseline is saved to tests/replay_baseline.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

# Ensure claude-hooks is on sys.path
_ROOT = Path(__file__).resolve().parents[1]
_SRC  = _ROOT / "src"
for _p in (_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

HOOKS_DB   = Path("~/Library/Mobile Documents/com~apple~CloudDocs/Databases/claude_hooks.sqlite").expanduser()
BASELINE   = Path(__file__).parent / "replay_baseline.json"

# Fields extracted from "UPS done" log line
_DONE_FIELDS = ["domains", "memories", "tools", "active_task", "related", "rag_chunks"]


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def _parse_ups_done(msg: str) -> dict | None:
    """Parse a 'UPS done: ...' dispatcher log message into a dict."""
    if not msg.startswith("UPS done:"):
        return None
    out = {}
    # session
    m = re.search(r"session=(\S+)", msg)
    out["session"] = m.group(1) if m else ""
    # memories count
    m = re.search(r"memories=(\d+)", msg)
    out["memories_count"] = int(m.group(1)) if m else 0
    # tools count
    m = re.search(r"tools=(\d+)", msg)
    out["tools_count"] = int(m.group(1)) if m else 0
    # active_task
    m = re.search(r"active_task=(\S*)", msg)
    out["active_task"] = m.group(1) if m else ""
    # domains list
    m = re.search(r"domains=(\[.*?\])", msg)
    try:
        out["domains"] = json.loads(m.group(1).replace("'", '"')) if m else []
    except Exception:
        out["domains"] = []
    # related list
    m = re.search(r"related=(\[.*?\])", msg)
    try:
        out["related"] = json.loads(m.group(1).replace("'", '"')) if m else []
    except Exception:
        out["related"] = []
    # rag_chunks list
    m = re.search(r"rag_chunks=(\[.*?\])", msg)
    try:
        out["rag_chunks"] = json.loads(m.group(1).replace("'", '"')) if m else []
    except Exception:
        out["rag_chunks"] = []
    return out


def _parse_ups_enter(msg: str) -> dict | None:
    """Parse a 'UPS enter: ...' dispatcher log message into a dict."""
    if not msg.startswith("UPS enter:"):
        return None
    out = {}
    m = re.search(r"session=(\S+)", msg)
    out["session"] = m.group(1) if m else ""
    m = re.search(r"cwd=(\S+)", msg)
    out["cwd"] = m.group(1) if m else ""
    m = re.search(r"prompt_len=(\d+)", msg)
    out["prompt_len"] = int(m.group(1)) if m else 0
    m = re.search(r"active_task=(\S+)", msg)
    out["active_task"] = m.group(1) if m else ""
    return out


def load_ups_events(since: str, limit: int) -> list[dict]:
    """Load UPS enter+done pairs from the hooks DB."""
    conn = sqlite3.connect(str(HOOKS_DB))
    rows = conn.execute("""
        SELECT id, ts, message FROM hook_logs
        WHERE logger = 'dispatcher'
          AND (message LIKE 'UPS enter%' OR message LIKE 'UPS done%')
          AND message NOT LIKE '%session=test%'
          AND message NOT LIKE '%session=sess-%'
          AND ts >= ?
        ORDER BY id ASC
    """, (since,)).fetchall()
    conn.close()

    # Pair enter + done by proximity (done always follows enter)
    events = []
    pending_enter: dict | None = None
    for row_id, ts, msg in rows:
        if msg.startswith("UPS enter:"):
            pending_enter = _parse_ups_enter(msg)
            if pending_enter:
                pending_enter["ts"] = ts
        elif msg.startswith("UPS done:") and pending_enter:
            done = _parse_ups_done(msg)
            if done and done["session"] == pending_enter["session"]:
                events.append({**pending_enter, **done})
                pending_enter = None

    # Filter to real sessions (8-char hex prefix typical)
    events = [e for e in events if len(e.get("session", "")) >= 8]

    # Deduplicate: keep only the last UPS event per session
    seen: dict[str, dict] = {}
    for e in events:
        seen[e["session"]] = e
    events = list(seen.values())

    return events[-limit:]  # most recent N unique sessions


# ---------------------------------------------------------------------------
# Graph replay
# ---------------------------------------------------------------------------

def _build_graph():
    import langchain_learning.session_graph as sg
    from langgraph.checkpoint.memory import MemorySaver
    sg._graph = sg.build_session_graph(checkpointer=MemorySaver())
    return sg


def _synthetic_prompt(prompt_len: int) -> str:
    """Produce a synthetic prompt of approximately the right length."""
    base = "replay harness test prompt "
    return (base * ((prompt_len // len(base)) + 1))[:max(prompt_len, 10)]


def replay_event(sg, event: dict) -> dict:
    """Run one UPS event through the current graph; return extracted output fields."""
    prompt = _synthetic_prompt(event.get("prompt_len", 20))
    cwd_str = event.get("cwd", "")
    # Resolve cwd: short names like 'claude-hooks' → full path
    if cwd_str and not cwd_str.startswith("/"):
        cwd_str = str(Path("~/workspace").expanduser() / cwd_str)

    # Fresh session per event (no checkpoint bleed between replays). Prefixed
    # with TEST_SESSION_PREFIX (task:a10a6638) so this routes to the isolated
    # test graph (task:b63088a1) instead of writing into the shared production
    # MemorySaver — replay runs used to leave permanent 'replay-<session>'
    # threads sitting in production forever (MemorySaver never evicts).
    from langchain_learning.session_graph import TEST_SESSION_PREFIX
    session_id = f"{TEST_SESSION_PREFIX}replay-{event['session'][:8]}"

    # active_task seeding is gone (task:882d67fa) — the task-aware nodes it fed
    # (load_related_tasks, load_task_code, load_task_history) are all gone too;
    # task-framework owns active-task context now, not this graph.
    result = sg.run_session(prompt=prompt, session_id=session_id, cwd=cwd_str)

    return {
        "session":        event["session"],
        "cwd":            event.get("cwd", ""),
        "domains":        result.get("domains", []),
        "memories_count": len(result.get("memories", [])),
        "tools_count":    len(result.get("tool_hints", [])),
    }


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

_DIFF_KEYS = ["domains", "memories_count", "tools_count"]


def diff_events(baseline: list[dict], replayed: list[dict]) -> list[dict]:
    """Compare baseline vs replayed on matching sessions."""
    base_map = {e["session"]: e for e in baseline}
    rep_map  = {e["session"]: e for e in replayed}

    deviations = []
    for session, base in base_map.items():
        rep = rep_map.get(session)
        if not rep:
            continue
        diffs = {}
        for key in _DIFF_KEYS:
            bv, rv = base.get(key), rep.get(key)
            if bv != rv:
                diffs[key] = {"baseline": bv, "replayed": rv}
        if diffs:
            deviations.append({"session": session, "cwd": base.get("cwd"), "diffs": diffs})
    return deviations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_ACTIVE_SESSION_THRESHOLD_MINUTES = 30


def _active_session_ids() -> set[str]:
    """Return session IDs whose last UPS event is within the active threshold.

    These sessions are still in-progress — their tool_hints scores are a moving
    target, so including them in the baseline causes spurious drift on replay.
    """
    conn = sqlite3.connect(str(HOOKS_DB))
    rows = conn.execute("""
        SELECT substr(message, instr(message, 'session=') + 8, 36) AS raw_sid,
               MAX(ts) AS last_ts
        FROM hook_logs
        WHERE message LIKE 'UPS enter%'
        GROUP BY raw_sid
    """).fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    active: set[str] = set()
    for raw_sid, ts_str in rows:
        sid = raw_sid.split()[0]  # strip trailing payload that follows the UUID
        try:
            ts = datetime.fromisoformat(ts_str.replace(" ", "T") + "+00:00")
            age_minutes = (now - ts).total_seconds() / 60
            if age_minutes < _ACTIVE_SESSION_THRESHOLD_MINUTES:
                active.add(sid)
        except ValueError:
            pass
    return active


def cmd_capture(args):
    print(f"Loading UPS events since {args.since}, limit={args.limit}...")
    events = load_ups_events(args.since, args.limit)
    print(f"  Found {len(events)} events.")

    active_ids = _active_session_ids()
    skipped = [e for e in events if e["session"] in active_ids]
    events   = [e for e in events if e["session"] not in active_ids]
    if skipped:
        print(f"  Skipped {len(skipped)} active session(s): {[s['session'][:8] for s in skipped]}")

    print("Building graph...")
    sg = _build_graph()

    print("Replaying events to capture baseline...")
    baseline = []
    for i, event in enumerate(events, 1):
        try:
            out = replay_event(sg, event)
            baseline.append(out)
            print(f"  [{i}/{len(events)}] session={event['session'][:8]} "
                  f"active_task={event.get('active_task','')[:8] or '-'} "
                  f"domains={out['domains']} memories={out['memories_count']} "
                  f"related={out['related']}")
        except Exception as exc:
            print(f"  [{i}/{len(events)}] session={event['session'][:8]} ERROR: {exc}")

    BASELINE.write_text(json.dumps(baseline, indent=2))
    print(f"\nBaseline saved → {BASELINE} ({len(baseline)} entries)")
    return baseline


def cmd_replay(baseline: list[dict], args):
    print(f"Loading UPS events since {args.since}, limit={args.limit}...")
    events = load_ups_events(args.since, args.limit)
    print(f"  Found {len(events)} events.")

    print("Building graph...")
    sg = _build_graph()

    print("Replaying events...")
    replayed = []
    for i, event in enumerate(events, 1):
        try:
            out = replay_event(sg, event)
            replayed.append(out)
        except Exception as exc:
            print(f"  [{i}/{len(events)}] session={event['session'][:8]} ERROR: {exc}")

    deviations = diff_events(baseline, replayed)
    matched = len([e for e in replayed if e["session"] in {b["session"] for b in baseline}])

    print(f"\n{'='*60}")
    print(f"Replayed {len(replayed)} events, {matched} matched baseline sessions")
    print(f"Deviations: {len(deviations)}")
    if deviations:
        print()
        for d in deviations:
            print(f"  ✗ session={d['session'][:8]} cwd={d['cwd']}")
            for key, vals in d["diffs"].items():
                print(f"      {key}: {vals['baseline']} → {vals['replayed']}")
    else:
        print("  ✓ All matched baseline")
    print(f"{'='*60}")
    return deviations


def main():
    parser = argparse.ArgumentParser(description="Replay harness for claude-hooks")
    parser.add_argument("--capture", action="store_true", help="Capture baseline from logs")
    parser.add_argument("--replay",  action="store_true", help="Replay and diff vs baseline")
    parser.add_argument("--since",   default="2026-06-13", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--limit",   type=int, default=100, help="Max events to process")
    args = parser.parse_args()

    if not args.capture and not args.replay:
        parser.print_help()
        sys.exit(1)

    baseline = None

    if args.capture:
        baseline = cmd_capture(args)

    if args.replay:
        if baseline is None:
            if not BASELINE.exists():
                print(f"No baseline found at {BASELINE}. Run --capture first.")
                sys.exit(1)
            baseline = json.loads(BASELINE.read_text())
            print(f"Loaded baseline: {len(baseline)} entries from {BASELINE}")
        cmd_replay(baseline, args)


if __name__ == "__main__":
    main()
