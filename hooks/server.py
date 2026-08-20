"""FastAPI hook server — persistent process replacing per-invocation subprocess dispatcher.

Routes: POST /hook/{event} for UserPromptSubmit | PreToolUse | PostToolUse | Stop | SessionStart | SessionEnd
State:  MemorySaver (in-process, in-memory) — does NOT survive server restarts.
        task:b3964f85 — replaced SqliteSaver (~/.claude/langgraph_checkpoints.db) after
        two corruption incidents (a 1.7GB .old-bloated file, then a
        "database disk image is malformed" failure that silently broke every
        Stop-hook write). Per-thread checkpoint history is capped at 5000 rows,
        enforced from NoopNode on every Stop event (langchain_learning/nodes/noop.py)
        — MemorySaver has no built-in eviction, so without this a long-running
        session would grow RAM usage unboundedly instead of corrupting a file.
Launch: uvicorn hooks.server:app --host 127.0.0.1 --port 8766

Subprocess dispatcher (dispatcher.py) remains untouched for fallback / testing.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from hooks.paths import PROJECT_ROOT as _PROJECT_ROOT, HOOKS_DIR as _HOOKS_DIR
for _p in (str(_PROJECT_ROOT), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.logger import get_logger, setup

log = get_logger(__name__)
_slog = setup("server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    from langgraph.checkpoint.memory import MemorySaver
    import langchain_learning.session_graph as sg
    import os as _os_mod

    pid = _os_mod.getpid()
    loop_impl = type(asyncio.get_running_loop()).__module__
    log.info("hook-server[pid=%d]: lifespan startup begin, event_loop=%s", pid, loop_impl)
    checkpointer = MemorySaver()
    sg._graph = sg.build_session_graph(checkpointer=checkpointer)
    import hooks.server_memory as server_memory
    server_memory.load()
    log.info("hook-server[pid=%d]: started, graph built with MemorySaver, server_session=%s", pid, server_memory.SERVER_SESSION_ID)
    yield
    log.info("hook-server[pid=%d]: shutdown, in-memory checkpoint state discarded", pid)
    sg._graph = None


app = FastAPI(lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.error("unhandled exception: %s", exc, exc_info=True)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


async def _safe_json(request: Request) -> dict:
    """Parse the request body as JSON, failing open to {} on malformed/empty
    bodies instead of letting the exception surface as a 500 — matches
    hooks/client.py's own fail-open philosophy for hook payloads (found via
    log audit 2026-07-26: an empty/malformed body previously crashed with an
    unhandled JSONDecodeError)."""
    try:
        return await request.json()
    except Exception as exc:
        log.warning("malformed request body on %s: %s", request.url.path, exc)
        return {}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log method, path, status, and elapsed ms for every request via the bare 'server' logger."""
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed = int((time.perf_counter() - t0) * 1000)
    _slog.info("HTTP %s %s → %d  %dms", request.method, request.url.path, response.status_code, elapsed)
    return response


@app.post("/hook/UserPromptSubmit")
async def user_prompt_submit(request: Request):
    """UserPromptSubmit hook — runs the full UPS LangGraph chain.

    Injects memories, tool hints, domain classification, and task context into the session
    checkpoint. Returns hookSpecificOutput.additionalSystemPrompt for Claude to consume.
    All lc.* node logs write immediately to claude_hooks.sqlite via SQLiteHandler.
    """
    from hooks.dispatcher import _handle_user_prompt_submit, _extract_prompt
    body = await _safe_json(request)
    result = _handle_user_prompt_submit(body)
    try:
        import hooks.server_memory as server_memory
        server_memory.record_prompt(body.get("session_id", ""), _extract_prompt(body))
    except Exception as exc:
        log.warning("server_memory: record_prompt failed: %s", exc)
    return JSONResponse(content=result or {})


@app.post("/hook/PreToolUse")
async def pre_tool_use(request: Request):
    """PreToolUse hook — runs gate_check node against the current session checkpoint.

    Returns permissionDecision=deny with a reason if a gated tool (e.g. imessage__send)
    is called without its prereq (e.g. contacts__search) in the session. Returns {} to
    allow. Gate internals (name_arg_check, ALLOW/DENY rows, prompt_id correlation)
    write immediately to claude_hooks.sqlite via SQLiteHandler.
    """
    from hooks.dispatcher import _handle_pre_tool_use
    body = await _safe_json(request)
    result = _handle_pre_tool_use(body)
    return JSONResponse(content=result or {})


@app.post("/hook/PostToolUse")
async def post_tool_use(request: Request):
    """PostToolUse hook — runs log_tool_usage node and conditional task-lifecycle bridge nodes.

    Upserts tool hint row in tool_hints.sqlite (skipped for test sessions).
    Bridge nodes fire when tool_name matches a lifecycle tool (tasks__set_active,
    tasks__pop_active, tasks__clear_active, tasks__finish, tasks__add_decision) —
    they write task activation state into the MemorySaver checkpoint so the next
    UPS turn sees the updated active task. Always returns {}.
    """
    from hooks.dispatcher import _handle_post_tool_use
    body = await _safe_json(request)
    result = _handle_post_tool_use(body)
    try:
        import hooks.server_memory as server_memory
        server_memory.record_tool_from_hook(body)
        server_memory.record_task_from_hook(body)
    except Exception as exc:
        log.warning("server_memory: record failed: %s", exc)
    return JSONResponse(content=result or {})


@app.post("/hook/Stop")
async def stop(request: Request):
    """Stop hook — finalises the *turn*, NOT the session.

    Fires at the end of every assistant response. Clears per-turn ephemeral fields
    (via run_stop) but must NOT evict the checkpoint — that would wipe cross-turn
    state (active task, turn counter) every turn. Session eviction happens on
    SessionEnd. Normally returns {}; returns a one-shot decision:"block" +
    sound-alert reason on the first Stop of a turn (see NoopNode).
    """
    from hooks.dispatcher import _handle_stop
    body = await _safe_json(request)
    result = _handle_stop(body)
    return JSONResponse(content=result or {})


@app.post("/hook/SessionStart")
async def session_start(request: Request):
    """SessionStart hook — logs each new or resumed session."""
    body = await _safe_json(request)
    from hooks.dispatcher import _handle_session_start
    _handle_session_start(body)
    return JSONResponse(content={})


@app.post("/hook/SessionEnd")
async def session_end(request: Request):
    """SessionEnd hook — the session has actually closed; evict its checkpoint.

    This is the correct place to reclaim MemorySaver storage (fires once when the
    session ends, unlike Stop which fires every turn). Always returns {}.
    """
    body = await _safe_json(request)
    from hooks.dispatcher import _handle_session_end
    _handle_session_end(body)
    return JSONResponse(content={})


@app.get("/health")
async def health():
    """Health check — returns status=ok."""
    return {"status": "ok"}


@app.get("/session/current")
async def session_current():
    """Current session_id — from the single most-recent checkpoint write, no active
    task required. /session/active (which task is active in this checkpoint) was
    removed (task:8529435a) — task-framework owns that fact; ask tasks__active.
    Returns {} if no checkpoint exists yet.
    """
    from hooks.session_state import get_current_session
    return JSONResponse(content=get_current_session())


@app.post("/set-active-taskid")
async def set_active_taskid(request: Request):
    """Push endpoint (task:996cc8f0) — task-framework's tasks__set_active and
    tasks__clear_active call this whenever the active task for a workspace
    changes, best-effort. Body: {workspace, task_id, title}. Empty/missing
    task_id clears the stored entry for that workspace.

    In-memory only, like every other bit of session state this server holds —
    task-framework remains the durable source of truth; this is just a live
    cache of the last thing it reported, discarded on restart.
    """
    body = await _safe_json(request)
    from hooks.session_state import set_active_task
    set_active_task(body.get("workspace", ""), body.get("task_id", ""), body.get("title", ""))
    return {"ok": True}


@app.get("/session/active-task")
async def session_active_task(workspace: str = ""):
    """Last active-task state pushed for a workspace via POST /set-active-taskid.

    Returns {workspace, task_id, title, ts} or {} if nothing has been pushed
    for it (including when workspace is omitted).
    """
    from hooks.session_state import get_active_task
    return JSONResponse(content=get_active_task(workspace))


@app.get("/cache")
async def cache_list():
    """Overview of every in-process cache in hooks/cache_store.py — name -> keys.

    Content-free by design (dropped the values) so this stays cheap to poll;
    fetch GET /cache/{name} for the actual content of one cache.
    """
    from hooks.cache_store import list_caches
    return JSONResponse(content=list_caches())


@app.get("/cache/{name}")
async def cache_get(name: str):
    """Current content of one named cache from hooks/cache_store.py.

    Returns {} for a name that doesn't exist yet — same as an empty cache,
    since get_cache() creates caches lazily on first use.
    """
    from hooks.cache_store import get_cache
    return JSONResponse(content=get_cache(name))


@app.get("/session/live")
async def session_live():
    """Live claude CLI processes — OS-level, not checkpoint-based.

    Returns {count, sessions: [{pid, etime}]} for every running `claude` CLI
    process (ps-based, matches noop.py's passive Stop-time check). Unlike
    /session and /session/{id} (which reflect the in-memory MemorySaver and
    can hold stale entries across a server restart, or miss a session that
    predates the current server process), this reflects actual live OS
    processes — the same check that caught a 20-day-old forgotten tmux
    session via a runaway sound-alert loop.
    """
    from langchain_learning.nodes.noop import live_claude_sessions
    sessions = live_claude_sessions()
    return JSONResponse(content={
        "count": len(sessions),
        "sessions": [{"pid": pid, "etime": etime} for pid, etime in sessions],
    })


@app.get("/session/memory")
async def session_memory(n_events: int = 50):
    """Server session memory — last N events from the unified chronological timeline.

    Free-flowing event log: prompts, tool calls, task activations, and assistant
    turns interleaved with timestamps. Durable across reloads (SQLite-backed).
    """
    import hooks.server_memory as server_memory
    return server_memory.get_server_memory(n_events=n_events)






@app.get("/session/{session_id}")
async def session_detail(session_id: str):
    """Full checkpoint state for one session — latest channel_values from the live MemorySaver.

    Returns {session_id, turn_count, state} where state is the checkpoint's channel_values
    dict (turn, domains, etc.) as of the most recent write for this thread.
    Returns {"detail": "not found"} (404) if no checkpoint exists for session_id.

    Routes through get_session_graph(session_id) (task:b63088a1) so a
    TEST_SESSION_PREFIX'd session_id resolves against the isolated test graph
    it was actually written to, not the production one.
    """
    import langchain_learning.session_graph as sg
    checkpointer = sg.get_session_graph(session_id).checkpointer

    turn_count = 0
    latest_state: dict = {}
    found = False
    for tup in checkpointer.list({"configurable": {"thread_id": session_id}}):
        found = True
        turn_count += 1
        latest_state = tup.checkpoint.get("channel_values", {})

    if not found:
        return JSONResponse(content={"detail": "not found"}, status_code=404)

    return JSONResponse(content={
        "session_id": session_id,
        "turn_count": turn_count,
        "state": latest_state,
    })


@app.get("/session")
async def session():
    """Session list — returns all sessions with checkpoint counts from the live checkpointer."""
    import langchain_learning.session_graph as sg
    checkpointer = sg._graph.checkpointer if sg._graph else None
    counts: dict[str, int] = {}
    if checkpointer:
        try:
            for tup in checkpointer.list(None):
                sid = tup.config["configurable"]["thread_id"]
                counts[sid] = counts.get(sid, 0) + 1
        except Exception:
            pass
    sessions = [{"session_id": sid, "turns": n} for sid, n in counts.items()]
    return {"count": len(sessions), "sessions": sessions}
