"""FastAPI hook server — persistent process replacing per-invocation subprocess dispatcher.

Routes: POST /hook/{event} for UserPromptSubmit | PreToolUse | PostToolUse | Stop | SessionStart | SessionEnd
State:  hooks/session_store.py — a per-session dict, in-process, lost on a server restart
        (task:31fb711d removed the LangGraph graph and its MemorySaver checkpointer).
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

import threading
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.logger import get_logger, setup

log = get_logger(__name__)
_slog = setup("server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    import os as _os_mod

    pid = _os_mod.getpid()
    loop_impl = type(asyncio.get_running_loop()).__module__
    log.info("hook-server[pid=%d]: lifespan startup begin, event_loop=%s", pid, loop_impl)
    import hooks.server_memory as server_memory
    server_memory.load()
    log.info("hook-server[pid=%d]: started, server_session=%s", pid, server_memory.SERVER_SESSION_ID)
    await run_in_threadpool(_warm_memory_connection)
    yield
    log.info("hook-server[pid=%d]: shutdown, in-memory session state discarded", pid)
    from hooks import session_store
    session_store.reset()
    from langchain_learning import memory_conn
    memory_conn.reset()


def _warm_memory_connection() -> None:
    """Open the shared MEMORY.sqlite connection at startup, so the first prompt
    does not pay the ~300 ms SQLCipher key derivation (task:bc39e90e), and build
    the BM25 scoring index over it (task:10ff74e3). The index build is ~20 ms;
    most of what this saves is the lazy import of the scorer (rank_bm25 pulls in
    numpy), which the first prompt's retrieve otherwise paid. Fail-open."""
    try:
        from langchain_learning.memory_conn import memory_connection
        t0 = time.perf_counter()
        with memory_connection() as conn:
            conn.execute("SELECT 1").fetchone()
            t1 = time.perf_counter()
            log.info("hook-server: memory connection warmed in %.0f ms", (t1 - t0) * 1000)
            from langchain_learning.nodes import _memory_scoring as ms
            ms.load_scoring_cfg()
            ms._bm25_index(conn)
            log.info("hook-server: BM25 index warmed in %.0f ms", (time.perf_counter() - t1) * 1000)
    except Exception as exc:
        log.warning("hook-server: memory warm-up failed: %s", exc)


# --- Hook handlers run off the event loop (task:bc39e90e) -------------------
# The dispatcher handlers are synchronous (SQLite, the memory scorer). Called
# straight from an `async def` route they ran on the event loop, so one slow
# UserPromptSubmit stalled every other session's PreToolUse. Each hook route now
# hands its handler to the threadpool, under a lock per session: session_store's
# read-modify-write sequences span several calls, and the event loop used to serialise same-session events
# (parallel tool calls fire concurrent PostToolUse hooks) as a side effect. The
# lock keeps that ordering inside a session while sessions run in parallel.
# A session's lock is dropped on SessionEnd; one that never ends leaves a bare Lock
# behind (a few hundred bytes per session per server lifetime), which is cheaper
# than an eviction that could hand two threads different locks for one session.
_session_locks: dict[str, threading.Lock] = {}


def _run_serialised(session_id: str, fn, body: dict):
    # dict.setdefault is atomic under the GIL: two threads always get the same Lock.
    with _session_locks.setdefault(session_id, threading.Lock()):
        return fn(body)


async def _run_hook(fn, body: dict):
    """Run a synchronous hook body in the threadpool, serialised per session."""
    return await run_in_threadpool(_run_serialised, str(body.get("session_id") or ""), fn, body)


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
    """UserPromptSubmit hook — begins the turn in session_store, then runs load_memories.

    Returns hookSpecificOutput.additionalContext with the turn state, the rendered
    user context and the BM25-scored memories. Tool hints, domain classification and
    task context are no longer injected (dispatcher._format_system_prompt tombstone;
    task-framework's own hook injects the active task). Records the prompt to the
    server_memory timeline.
    All lc.* node logs write immediately to claude_hooks.sqlite via SQLiteHandler.
    """
    body = await _safe_json(request)
    result = await _run_hook(_user_prompt_submit_sync, body)
    return JSONResponse(content=result or {})


def _user_prompt_submit_sync(body: dict):
    from hooks.dispatcher import _handle_user_prompt_submit, _extract_prompt
    result = _handle_user_prompt_submit(body)
    try:
        import hooks.server_memory as server_memory
        server_memory.record_prompt(body.get("session_id", ""), _extract_prompt(body))
    except Exception as exc:
        log.warning("server_memory: record_prompt failed: %s", exc)
    return result


@app.post("/hook/PreToolUse")
async def pre_tool_use(request: Request):
    """PreToolUse hook — runs the gates (hooks.gates.check) with the turn's prompt_id from session_store.

    Returns permissionDecision=deny with a reason if a git commit carries no task:<id>
    (GitCommitGate / GitCommitMcpGate), or — before the gates — if a WebSearch query has a
    fresh cached result, which is returned as the deny reason (src/tools/web_search_cache.py).
    Returns {} to allow. A handler error is a
    500, which hooks/client.py turns into {} (allow); nothing fails closed here
    (task:4e56d23a). Gate internals (name_arg_check, ALLOW/DENY rows, prompt_id correlation)
    write immediately to claude_hooks.sqlite via SQLiteHandler.
    """
    from hooks.dispatcher import _handle_pre_tool_use
    body = await _safe_json(request)
    result = await _run_hook(_handle_pre_tool_use, body)
    return JSONResponse(content=result or {})


@app.post("/hook/PostToolUse")
async def post_tool_use(request: Request):
    """PostToolUse hook — runs the dispatcher's side effects, then the log_tool_usage node (a plain call, not a graph node).

    Side effects (each fail-open): WebSearch cache store, find_cli log and prompt-shadow
    linking, vault reindex, context-size nudge, and skill-tagged memories / skill-tag
    nudge. log_tool_usage upserts the tool hint row in tool_hints.sqlite (skipped for
    test sessions and memory tools). Returns {} or a hookSpecificOutput.additionalContext
    from the nudge or skill-memory side effects — which return early, so log_tool_usage
    is skipped for that call. Records the call to the server_memory timeline.
    """
    body = await _safe_json(request)
    result = await _run_hook(_post_tool_use_sync, body)
    return JSONResponse(content=result or {})


def _post_tool_use_sync(body: dict):
    from hooks.dispatcher import _handle_post_tool_use
    result = _handle_post_tool_use(body)
    try:
        import hooks.server_memory as server_memory
        server_memory.record_tool_from_hook(body)
        server_memory.record_task_from_hook(body)
    except Exception as exc:
        log.warning("server_memory: record failed: %s", exc)
    return result


@app.post("/hook/Stop")
async def stop(request: Request):
    """Stop hook — finalises the *turn*, NOT the session.

    Fires at the end of every assistant response. Marks the Stop in session_store but
    must NOT evict the session — that would reset the turn counter every turn.
    Eviction happens on SessionEnd. Normally returns {}; plays the completion
    chime once per turn (hooks/stop_effects.py).
    """
    from hooks.dispatcher import _handle_stop
    body = await _safe_json(request)
    result = await _run_hook(_handle_stop, body)
    return JSONResponse(content=result or {})


@app.post("/hook/SessionStart")
async def session_start(request: Request):
    """SessionStart hook — logs each new or resumed session."""
    body = await _safe_json(request)
    from hooks.dispatcher import _handle_session_start
    await _run_hook(_handle_session_start, body)
    return JSONResponse(content={})


@app.post("/hook/SessionEnd")
async def session_end(request: Request):
    """SessionEnd hook — the session has actually closed; evict its session_store entry.

    This is the correct place to reclaim session storage (fires once when the
    session ends, unlike Stop which fires every turn). Always returns {}.
    """
    body = await _safe_json(request)
    await _run_hook(_session_end_sync, body)
    _session_locks.pop(str(body.get("session_id") or ""), None)
    return JSONResponse(content={})


def _session_end_sync(body: dict):
    from hooks.dispatcher import _handle_session_end
    _handle_session_end(body)
    try:
        import hooks.server_memory as server_memory
        server_memory.record_session_end(body.get("session_id", ""), body.get("reason", ""))
    except Exception as exc:
        log.warning("server_memory: record_session_end failed: %s", exc)


@app.get("/health")
async def health():
    """Health check — returns status=ok."""
    return {"status": "ok"}


@app.get("/session/current")
async def session_current():
    """Current session_id — from the most recently touched real session in
    session_store, no active task required. /session/active (which task is active in this checkpoint) was
    removed (task:8529435a) — task-framework owns that fact; ask tasks__active.
    Returns {} if no session exists yet.
    """
    from hooks.session_state import get_current_session
    return JSONResponse(content=get_current_session())

# POST /set-active-taskid + GET /session/active-task (task:996cc8f0) were removed
# (task:173e6846). task-framework stopped pushing active-task state to this server,
# and nothing here ever read the cache back into a turn — the '## Active task'
# injected block had already gone (task:8be768df). task-framework owns the active
# task; ask tasks__active.


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
    """Live claude CLI processes — OS-level, not session_store-based.

    Returns {count, sessions: [{pid, etime}]} for every running `claude` CLI
    process (ps-based, matches the Stop-time check in stop_effects.py). Unlike
    /session and /session/{id} (which reflect the in-memory session_store and
    can hold stale entries across a server restart, or miss a session that
    predates the current server process), this reflects actual live OS
    processes — the same check that caught a 20-day-old forgotten tmux
    session via a runaway sound-alert loop.
    """
    from hooks.stop_effects import live_claude_sessions
    sessions = live_claude_sessions()
    return JSONResponse(content={
        "count": len(sessions),
        "sessions": [{"pid": pid, "etime": etime} for pid, etime in sessions],
    })


@app.get("/session/memory")
async def session_memory(n_events: int = 50):
    """Server session memory — last N events from the unified chronological timeline.

    Free-flowing event log: prompts, tool calls, task activations, surfaced memories and session ends
    interleaved with timestamps — no assistant turns. Durable across reloads
    (SQLite-backed, rolling).
    """
    import hooks.server_memory as server_memory
    return server_memory.get_server_memory(n_events=n_events)


@app.get("/session/{session_id}")
async def session_detail(session_id: str):
    """State of one session from hooks/session_store.py.

    Returns {session_id, turn_count, state}; turn_count is the UserPromptSubmit
    counter. 404 {"detail": "not found"} when the server has no entry for it.
    """
    from hooks import session_store
    if not session_store.exists(session_id):
        return JSONResponse(content={"detail": "not found"}, status_code=404)
    state = session_store.get(session_id)
    return JSONResponse(content={"session_id": session_id, "turn_count": state["turn"], "state": state})


@app.get("/session")
async def session():
    """Session list — every session in session_store with its turn count."""
    from hooks import session_store
    sessions = [{"session_id": sid, "turns": st["turn"]} for sid, st in session_store.snapshot().items()]
    return {"count": len(sessions), "sessions": sessions}
