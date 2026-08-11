"""NoopNode — silent pass-through for stop and unknown event types."""
from __future__ import annotations

import subprocess

from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)

_SILENT_EVENTS = {"stop"}


def live_claude_sessions() -> list[tuple[str, str]]:
    """List (pid, elapsed) for every running `claude` CLI process.

    Matches on the basename of `comm` (e.g. .../resources/native-binary/claude)
    so MCP server subprocesses (python, node, uv) sharing the word "claude" in
    their path don't get counted. Fails open (empty list) — this is an
    observability nicety on the Stop hot path, not something that should ever
    block or slow down a turn's Stop response.

    Shared with hooks/server.py's GET /session/live — same check, reachable
    either passively (this module logs it on every Stop) or on demand (curl).
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,etime=,comm="],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception as exc:
        _log.warning("[noop] live session ps failed: %s", exc)
        return []

    sessions = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid, etime, comm = parts[0], parts[1], parts[-1]
        if comm.rsplit("/", 1)[-1] == "claude":
            sessions.append((pid, etime))
    return sessions


def _log_live_sessions_if_multiple() -> None:
    """Log a summary line when more than one `claude` session is live.

    Surfaces the count and each pid's elapsed runtime to hook_logs so a
    forgotten detached tmux/terminal session (task: stray 20-day-old claude
    process) shows up passively on every Stop instead of only being noticed
    when something like a runaway sound loop draws attention to it.
    """
    sessions = live_claude_sessions()
    if len(sessions) <= 1:
        return
    detail = ", ".join(f"pid={pid} etime={etime}" for pid, etime in sessions)
    _log.warning("[noop] live claude sessions=%d: %s", len(sessions), detail)

# task:b3964f85 — MemorySaver (which replaced SqliteSaver after two corruption
# incidents) has no built-in eviction: without this cap, a long-running
# session's per-thread checkpoint history now grows unboundedly in RAM instead
# of unboundedly on disk. checkpoint_ids are monotonically sortable strings
# (a LangGraph invariant), so keeping the lexicographically-largest
# _CHECKPOINT_ROW_CAP ids per (thread_id, checkpoint_ns) keeps the most recent
# history and evicts the oldest — costs time-travel/resume-from-old-turn
# ability for evicted checkpoints, never current-state correctness.
_CHECKPOINT_ROW_CAP = 5000


def _trim_thread_checkpoints(thread_id: str, row_cap: int = _CHECKPOINT_ROW_CAP) -> None:
    """Cap this thread's checkpoint history under the live MemorySaver.

    No-ops if the live graph/checkpointer isn't set (e.g. standalone/test
    invocations using get_session_graph()'s own throwaway MemorySaver with no
    prior history) or isn't a MemorySaver (defensive — this reaches into
    MemorySaver-specific internals, .storage/.writes, that no other
    checkpointer implementation exposes the same way).
    """
    import langchain_learning.session_graph as sg
    from langgraph.checkpoint.memory import MemorySaver

    graph = sg._graph
    if graph is None:
        return
    checkpointer = graph.checkpointer
    if not isinstance(checkpointer, MemorySaver):
        return

    thread_storage = checkpointer.storage.get(thread_id)
    if not thread_storage:
        return

    for ns, ns_checkpoints in thread_storage.items():
        if len(ns_checkpoints) <= row_cap:
            continue
        ids_sorted = sorted(ns_checkpoints.keys())  # oldest first
        evict_ids = ids_sorted[:-row_cap]
        for cid in evict_ids:
            del ns_checkpoints[cid]
            checkpointer.writes.pop((thread_id, ns, cid), None)
        _log.info(
            "[noop] checkpoint trim: thread=%s ns=%r evicted=%d kept=%d",
            thread_id[:8], ns, len(evict_ids), row_cap,
        )


class NoopNode:
    """No-op node routed to for stop events and unrecognised event types.

    Marks the first Stop event of a turn via stop_alert_sent, which gates
    PlaySoundNode (the next node in the stop chain) so the completion chime
    fires exactly once per turn. Does not touch Claude's response itself —
    the sound is a direct server-side side effect, not a blocked stop that
    makes Claude call a tool.

    Also caps this thread's checkpoint history (task:b3964f85) on every Stop,
    not just the first of a turn — MemorySaver has no built-in eviction, and
    Stop is the one event guaranteed to fire every turn regardless of
    stop_alert_sent state, mirroring how UserPromptSubmit's cross-session trim
    runs on every prompt rather than only once.

    Also logs a live-session-count warning on every Stop when more than one
    `claude` CLI process is running, with each pid's elapsed runtime — surfaces
    forgotten detached tmux/terminal sessions (see: a 20-day-old stray process
    found via a runaway sound-alert loop) passively instead of only on demand.

    Tags: fallback, event-routing, noop, checkpoint-trim, live-session-audit
    """

    def __call__(self, state: SessionState) -> dict:
        ev = state.get("event_type")
        if ev == "":
            # prewarm_session()'s deliberate no-op invocation (checkpointer
            # init only, before the first real hook event) -- expected on
            # every new session, not a routing anomaly.
            _log.info("[noop] prewarm session=%s",
                      (state.get("session_id") or "")[:8])
            return {}
        if ev not in _SILENT_EVENTS:
            _log.warning("[noop] unknown event_type=%r session=%s",
                         ev, (state.get("session_id") or "")[:8])
            return {}

        session_id = state.get("session_id") or ""
        if session_id:
            _trim_thread_checkpoints(session_id)

        _log_live_sessions_if_multiple()

        if state.get("stop_alert_sent"):
            return {}

        return {"stop_alert_sent": True}
