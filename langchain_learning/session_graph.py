"""Session State Machine — unified event graph.

One graph handles all four Claude Code hook events. Each event type routes to
its own chain of nodes, giving full observability across the session lifecycle
in a single graph topology.

Graph shape:

    START → route_event (conditional)
      ├── user_prompt_submit → load_turn ──(task active?)──► load_active_task → load_task_history
      │                         → load_task_code (TurboVec RAG) → load_related_tasks ──► cwd_domain_detect → load_memories
      │                                            └─(no task)────────────►
      │                         → score_tools → set_prompt_id → log_task_events → END
      ├── pre_tool_use       → gate_check → END
      ├── post_tool_use      → log_tool_usage → update_tool_keywords → (tasks__set_active → activate_task | tasks__clear_active/finish → deactivate_task | *) → END
      └── stop               → noop → play_sound → END

State persistence: the FastAPI hook server (hooks/server.py) opens a MemorySaver
at startup and passes it via build_session_graph(checkpointer=...). State is keyed
by session_id (thread_id) and evicted on SessionEnd. task:b3964f85 — does NOT
survive server restarts (in-memory only, replacing a SqliteSaver that corrupted
twice); per-thread checkpoint history is capped by NoopNode on every Stop event.

Node implementations live in langchain_learning/nodes/ — one class per file.
registry.py holds NODE_REGISTRY + get_node() factory.
"""
from __future__ import annotations

from collections import OrderedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END

from langchain_learning.nodes.registry import get_node
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

_EVENT_TYPES = {"user_prompt_submit", "pre_tool_use", "post_tool_use", "stop"}


def _route_event(state: SessionState) -> str:
    ev = state.get("event_type", "")
    return ev if ev in _EVENT_TYPES else "unknown"




# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_session_graph(checkpointer=None):
    """Construct and compile the unified session graph.

    Args:
        checkpointer: Optional LangGraph checkpointer (e.g. MemorySaver).
                      When provided, state is retained across invocations by thread_id.
                      When None (default/tests), each invoke is stateless.

    Tags: session-graph, LangGraph, StateGraph, event-routing, hooks
    """
    builder = StateGraph(SessionState)

    # Register all nodes from registry
    for name in [
        "noop",
        "load_turn", "load_active_task", "load_task_history", "load_task_code", "load_related_tasks", "load_related_commits", "load_memories",
        "cwd_domain_detect",
        "score_tools", "summarize_task_context", "set_prompt_id",
        "gate_check",
        "log_tool_usage",
        "activate_task", "deactivate_task", "decision_task",
        "mcp_hook_bridge",
        "backfill_memory_files",
        "log_task_events",
        "play_sound",
    ]:
        builder.add_node(name, get_node(name))

    # START → conditional route by event_type
    builder.add_conditional_edges(
        START,
        _route_event,
        {
            "user_prompt_submit": "load_turn",
            "pre_tool_use":       "gate_check",
            "post_tool_use":      "log_tool_usage",
            "stop":               "noop",
            "unknown":            "noop",
        },
    )

    # UserPromptSubmit chain
    builder.add_conditional_edges(
        "load_turn",
        lambda s: "load_active_task" if s.get("active_task_id") else "load_related_tasks",
        {
            "load_active_task": "load_active_task",
            "load_related_tasks": "load_related_tasks",
        },
    )
    # fan-out from load_active_task: history, code, related tasks, related commits run in parallel
    builder.add_edge("load_active_task",      "load_task_history")
    builder.add_edge("load_active_task",      "load_task_code")
    builder.add_edge("load_active_task",      "load_related_tasks")
    builder.add_edge("load_active_task",      "load_related_commits")
    # fan-in: all four loaders converge at summarize_task_context (compresses
    # task_context/rag_chunks/related_* into task_context_summary; first-turn-gated
    # internally, so it's a fast no-op pass-through on every other turn)
    for loader in ("load_task_history", "load_task_code", "load_related_tasks", "load_related_commits"):
        builder.add_edge(loader, "summarize_task_context")
    # fan-out from summarize_task_context to second-tier nodes
    builder.add_edge("summarize_task_context", "cwd_domain_detect")
    builder.add_edge("summarize_task_context", "load_memories")
    builder.add_edge("summarize_task_context", "score_tools")
    # fan-in: all three converge at set_prompt_id
    builder.add_edge("cwd_domain_detect",     "set_prompt_id")
    builder.add_edge("load_memories",         "set_prompt_id")
    builder.add_edge("score_tools",           "set_prompt_id")
    builder.add_edge("set_prompt_id",   "log_task_events")
    builder.add_edge("log_task_events", END)

    # PreToolUse chain
    builder.add_edge("gate_check", END)

    # PostToolUse chain
    def _post_tool_route(state: SessionState) -> str:
        tool = state.get("tool_name", "")
        if tool in ("tasks__set_active", "tasks__pop_active"):
            return "activate_task"
        if tool in ("tasks__clear_active", "tasks__finish"):
            return "deactivate_task"
        if tool == "tasks__add_decision":
            return "decision_task"
        # Generic: any MCP tool returning __hook__ gets bridge injection
        from langchain_learning.nodes._json_utils import extract_tool_result_json
        result = extract_tool_result_json(state.get("tool_result") or {})
        if result.get("__hook__"):
            return "mcp_hook_bridge"
        return END

    builder.add_conditional_edges(
        "log_tool_usage",
        _post_tool_route,
        {"activate_task": "activate_task", "deactivate_task": "deactivate_task",
         "decision_task": "decision_task", "mcp_hook_bridge": "mcp_hook_bridge", END: END},
    )
    # Backfill slot — single BackfillNodeProtocol node after activation.
    # To swap: replace this edge + the node registration with your own implementation.
    # Multiple strategies must be composed inside one node — do not add parallel edges.
    # See: langchain_learning/nodes/base.py BackfillNodeProtocol for the contract.
    builder.add_conditional_edges(
        "activate_task",
        lambda s: "backfill_memory_files" if s.get("task_files") else END,
        {"backfill_memory_files": "backfill_memory_files", END: END},
    )
    builder.add_edge("backfill_memory_files", END)
    builder.add_edge("deactivate_task",   END)
    builder.add_edge("decision_task",     END)
    builder.add_edge("mcp_hook_bridge",   END)

    # Fallback / Stop chain
    builder.add_edge("noop",            "play_sound")
    builder.add_edge("play_sound",      END)

    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_graph = None
_test_graph = None

# Integration tests use a session_id with this prefix so their checkpoint
# writes route to an isolated in-process graph/checkpointer instead of the
# shared production one (task:b63088a1). Before this, integration tests wrote
# real, permanently-unevicted threads straight into production's MemorySaver —
# the direct root cause of task:a4531510's stale-cross-thread bug. Both graphs
# reset on every server restart regardless (MemorySaver has no persistence and
# none is intended), so the test graph needs no separate eviction/cleanup —
# it's isolated by construction, not by a cleanup pass.
TEST_SESSION_PREFIX = "integration-test-"


def get_session_graph(session_id: str = ""):
    """Return the module-level graph for this session_id.

    Test-prefixed session_ids (TEST_SESSION_PREFIX) route to an isolated
    _test_graph, keeping integration-test traffic out of the real production
    checkpoint store entirely — same endpoints, same code path, isolated
    storage only (task:b63088a1).

    In production, _graph is set by the FastAPI server lifespan (MemorySaver).
    In tests, callers may inject _graph directly, or this falls back to a
    fresh MemorySaver graph per process (no disk needed, no teardown). The
    same lazy-fallback pattern applies to _test_graph.
    """
    global _graph, _test_graph
    if session_id.startswith(TEST_SESSION_PREFIX):
        if _test_graph is None:
            from langgraph.checkpoint.memory import MemorySaver
            _test_graph = build_session_graph(checkpointer=MemorySaver())
        return _test_graph
    if _graph is None:
        from langgraph.checkpoint.memory import MemorySaver
        _graph = build_session_graph(checkpointer=MemorySaver())
    return _graph


# ---------------------------------------------------------------------------
# Public entry points (one per hook event)
# ---------------------------------------------------------------------------

def _fresh_state(session_id: str) -> SessionState:
    """Full default state for the very first user_prompt_submit of a new session."""
    return SessionState(
        event_type="", prompt="", cwd="", session_id=session_id,
        turn=0,
        memories=[],
        domains=[], keywords=[], tool_hints=[],
        active_task_id="", active_task_title="", active_parent_task_id="", active_parent_task_title="", task_memories=[], task_context=[], task_rag_chunks=[], task_stack=[], mid_task_decisions=[], related_tasks=[], related_commits=[],
        current_state="prompt",
        tool_name="", tool_input={}, prompt_id="", prompt_tools=[],
        session_prompt_ids=[], session_tools=OrderedDict(), session_prompt_texts={},
        gate_denied=False, gate_reason="",
        duration_ms=0.0, tool_result={},
        pending_hook_output={},
        cwd_unmapped=False, cwd_domain_reminder_sent=False,
        stop_alert_sent=False,
        sound_played=False,
        # tool_use_id="",
    ) # type: ignore


def _config(session_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": session_id or "default"}}


def _base_state(session_id: str) -> SessionState:
    """Return checkpoint state for session_id, or a fresh state if none exists."""
    cfg = _config(session_id)
    existing = get_session_graph(session_id).get_state(cfg)  # type: ignore[arg-type]
    return existing.values if existing and existing.values else _fresh_state(session_id)  # type: ignore[return-value]


def run_session(prompt: str, session_id: str = "", cwd: str = "") -> SessionState:
    """UserPromptSubmit entry point.

    Seeds a fresh state only when no checkpoint exists for this session yet.
    On subsequent turns the checkpoint supplies all prior state; we inject
    only the event-specific inputs on top.
    """
    import time as _time
    t0 = _time.monotonic()
    state: SessionState = _base_state(session_id) | {"event_type": "user_prompt_submit", "prompt": prompt, "cwd": cwd, "session_id": session_id, "stop_alert_sent": False, "sound_played": False}  # type: ignore[operator]
    result = get_session_graph(session_id).invoke(state, config=_config(session_id))  # type: ignore[arg-type]
    # Surface-and-clear pending_hook_output (mirrors run_post_tool/run_stop):
    # the returned result keeps it for the UPS dispatcher to render this turn,
    # but the checkpoint copy is zeroed so it cannot leak into the next
    # PostToolUse response.
    if result.get("pending_hook_output"):
        get_session_graph(session_id).update_state(_config(session_id), {"pending_hook_output": {}})
    _log.info("UPS phase=done session=%s elapsed_ms=%.0f", (session_id or "")[:8], ((_time.monotonic() - t0) * 1000))
    return result


def run_gate(tool_name: str, tool_input: dict, session_id: str = "") -> dict:
    """PreToolUse entry point. Returns {gate_denied, gate_reason}.

    prompt_id flows from the checkpoint written by the prior user_prompt_submit.
    If no UPS checkpoint exists, a fallback prompt_id is generated so gate logs
    remain traceable even on first-turn tool calls.
    """
    import hashlib
    import time as _time

    cfg = _config(session_id)
    try:
        saved = get_session_graph(session_id).get_state(cfg)
        prompt = (saved.values.get("prompt") or "") if saved and saved.values else ""
    except Exception:
        prompt = ""
    base = _base_state(session_id)
    prompt_id = (base.get("prompt_id") or "")
    if not prompt_id:
        raw = f"{session_id or 'anon'}-{_time.time()}"
        prompt_id = "fb-" + hashlib.sha256(raw.encode()).hexdigest()[:12]
        _log.info("run_gate session=%s generated fallback prompt_id=%s (no UPS checkpoint)",
                  (session_id or "")[:8] or "?", prompt_id)
    state: SessionState = base | {"event_type": "pre_tool_use", "tool_name": tool_name, "tool_input": tool_input, "session_id": session_id, "prompt": prompt, "prompt_id": prompt_id}  # type: ignore[operator]
    result = get_session_graph(session_id).invoke(state, config=cfg)  # type: ignore[arg-type]
    get_session_graph(session_id).update_state(cfg, {"gate_denied": False, "gate_reason": "", "tool_name": "", "tool_input": {}})
    return {"gate_denied": result["gate_denied"], "gate_reason": result["gate_reason"]}


def run_post_tool(tool_name: str, tool_input: dict, session_id: str,
                  duration_ms: float = 0.0, tool_result: dict | None = None,
                  prompt: str = "") -> dict:
    """PostToolUse entry point.

    prompt_id flows from the checkpoint written by the prior user_prompt_submit.
    Returns pending_hook_output if a node set one (e.g. retrospective prompt), else {}.
    """
    cfg = _config(session_id)
    # Do not overwrite "prompt" with empty string — preserve checkpoint value for gate name checks
    state: SessionState = _base_state(session_id) | {"event_type": "post_tool_use", "tool_name": tool_name, "tool_input": tool_input, "tool_result": tool_result or {}, "session_id": session_id, "duration_ms": duration_ms}  # type: ignore[operator]
    if prompt:
        state = state | {"prompt": prompt}  # type: ignore[operator]
    get_session_graph(session_id).invoke(state, config=cfg)  # type: ignore[arg-type]
    final = get_session_graph(session_id).get_state(cfg)
    if final is None or not final.values:
        _log.warning("[run_post_tool] session=%s tool=%s — get_state returned empty, no hook_output", session_id[:8], tool_name)
        hook_output: dict = {}
    else:
        hook_output = final.values.get("pending_hook_output") or {}
        if hook_output:
            _log.info("[run_post_tool] session=%s tool=%s — returning hook_output keys=%s", session_id[:8], tool_name, list(hook_output.keys()))
    get_session_graph(session_id).update_state(cfg, {"tool_name": "", "tool_input": {}, "tool_result": {}, "duration_ms": 0.0, "pending_hook_output": {}})
    return hook_output


def prewarm_session(session_id: str) -> bool:
    """Initialise a checkpoint thread before the first UPS fires.

    Returns True if this is a new session, False if a checkpoint already existed.
    Invokes the graph with event_type="" which routes to the noop node — only
    the checkpointer write overhead, no memory/tool/RAG nodes execute.
    """
    cfg = _config(session_id)
    existing = get_session_graph(session_id).get_state(cfg)
    if existing and existing.metadata is not None:
        return False
    state: SessionState = _fresh_state(session_id) | {"event_type": ""}  # type: ignore[operator]
    get_session_graph(session_id).invoke(state, config=cfg)  # type: ignore[arg-type]
    return True


def run_stop(session_id: str) -> dict:
    """Stop hook entry point.

    Returns pending_hook_output if a node set one (e.g. NoopNode's one-shot
    sound alert), else {}.
    """
    cfg = _config(session_id)
    state: SessionState = _base_state(session_id) | {"event_type": "stop", "session_id": session_id}  # type: ignore[operator]
    get_session_graph(session_id).invoke(state, config=cfg)  # type: ignore[arg-type]
    final = get_session_graph(session_id).get_state(cfg)
    hook_output: dict = (final.values.get("pending_hook_output") or {}) if final and final.values else {}
    get_session_graph(session_id).update_state(cfg, {
        "event_type": "",
        "prompt": "", "prompt_id": "", "prompt_tools": [],
        "tool_name": "", "tool_input": {}, "tool_result": {}, "duration_ms": 0.0,
        "gate_denied": False, "gate_reason": "",
        "pending_hook_output": {},
    })
    return hook_output

