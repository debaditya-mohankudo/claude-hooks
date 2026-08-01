"""Server session memory — durable consolidated context store (SQLite + in-memory session).

Answers "what was I working on?" with one consolidated view: recent prompts, MCP
tool calls, and activated tasks as a real chronological event sequence with
timestamps. Deliberately redundant with task_events / hook logs; the value is
consolidation in one queryable place.

Two layers:
  - SQLite (~/.claude/server_memory.sqlite) — durable backing, capped rolling window.
  - In-memory session cache — fast reads; hydrated from SQLite on server startup
    (ServerMemory.load()), so a reload/restart keeps the context. Writes are
    write-through (cache + DB).

Schema is a single event table: a `type` discriminator ('prompt'|'tool'|'task'),
a shared `content` column (prompt text / tool short-name / task title), and `ref`
(task_id for tasks). One ORDER BY = the interleaved timeline.

SERVER_SESSION_ID tags each row with the writing run; it is not a lifecycle
boundary — rows from many runs coexist.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from src.logger import get_logger

_log = get_logger(__name__)

SERVER_SESSION_ID = uuid.uuid4().hex[:12]
STARTED_AT = time.time()

# Skip test sessions so the durable store isn't polluted (past_mistakes.md #5).
_TEST_PREFIXES = ("test-", "pytest-", "api-test-")


class ServerMemory:
    """SQLite-backed consolidated context store with a hydrated in-memory session."""

    _DB = Path.home() / ".claude" / "server_memory.sqlite"
    _MAX_ENTRIES = 1000          # rolling window — newest N events kept
    _cache: list[dict] = []      # in-memory session: chronological event dicts

    # ── storage ──────────────────────────────────────────────────────────────

    @classmethod
    def _connect(cls) -> sqlite3.Connection:
        conn = sqlite3.connect(str(cls._DB), timeout=5)
        # Migrate the (ephemeral, capped) store if it predates the type/content schema.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(server_memory)")}
        if cols and "type" not in cols:
            conn.execute("DROP TABLE server_memory")
            conn.commit()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS server_memory (
                   id                INTEGER PRIMARY KEY,
                   server_session_id TEXT,
                   claude_session_id TEXT,
                   ts                REAL,
                   type              TEXT,   -- 'prompt' | 'tool' | 'task'
                   content           TEXT,   -- prompt text / tool short-name / task title
                   ref               TEXT,   -- task_id for tasks; NULL otherwise
                   args              TEXT    -- MCP tool input args as compact JSON; NULL otherwise
               )"""
        )
        # Migrate older rows that predate the args column.
        if cols and "args" not in cols:
            conn.execute("ALTER TABLE server_memory ADD COLUMN args TEXT")
            conn.commit()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sm_ts ON server_memory(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sm_type ON server_memory(type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sm_session ON server_memory(claude_session_id)")
        return conn

    @classmethod
    def load(cls) -> None:
        """Hydrate the in-memory session from SQLite — call at server startup/reload."""
        try:
            conn = cls._connect()
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT claude_session_id, ts, type, content, ref, args FROM server_memory "
                    "ORDER BY id DESC LIMIT ?",
                    (cls._MAX_ENTRIES,),
                ).fetchall()
            finally:
                conn.close()
            cls._cache = [dict(r) for r in reversed(rows)]
            _log.info("[server_memory] loaded %d events from %s", len(cls._cache), cls._DB)
        except Exception as exc:
            _log.warning("[server_memory] load failed: %s", exc)
            cls._cache = []

    @classmethod
    def _insert(cls, claude_session_id: str, *, type: str, content: str, ref: str | None = None, args: str | None = None) -> None:
        if (claude_session_id or "").startswith(_TEST_PREFIXES):
            return
        ev = {
            "claude_session_id": claude_session_id or "",
            "ts": time.time(),
            "type": type,
            "content": content,
            "ref": ref,
            "args": args,
        }
        # Write-through to SQLite (durable), then mirror into the in-memory session.
        try:
            conn = cls._connect()
            try:
                conn.execute(
                    """INSERT INTO server_memory
                       (server_session_id, claude_session_id, ts, type, content, ref, args)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (SERVER_SESSION_ID, ev["claude_session_id"], ev["ts"], type, content, ref, args),
                )
                conn.execute(
                    "DELETE FROM server_memory WHERE id NOT IN "
                    "(SELECT id FROM server_memory ORDER BY id DESC LIMIT ?)",
                    (cls._MAX_ENTRIES,),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            _log.warning("[server_memory] insert failed: %s", exc)
        cls._cache.append(ev)
        if len(cls._cache) > cls._MAX_ENTRIES:
            del cls._cache[:-cls._MAX_ENTRIES]

    # ── record ───────────────────────────────────────────────────────────────

    @classmethod
    def record_prompt(cls, claude_session_id: str, text: str) -> None:
        if text and not text.startswith("Summarize the following task context:"):
            cls._insert(claude_session_id, type="prompt", content=text)

    @classmethod
    def record_tool(cls, claude_session_id: str, tool: str, args: str | None = None) -> None:
        if tool:
            cls._insert(claude_session_id, type="tool", content=tool, args=args)

    @classmethod
    def record_task(cls, claude_session_id: str, task_id: str, title: str) -> None:
        if task_id:
            cls._insert(claude_session_id, type="task", content=title or "", ref=task_id)

    @classmethod
    def record_memories(cls, claude_session_id: str, names: list[str]) -> None:
        """Record which memory IDs were injected on this prompt turn."""
        if names:
            cls._insert(
                claude_session_id,
                type="memories",
                content=str(len(names)),
                args=json.dumps(names, separators=(",", ":")),
            )

    # ── read (from the in-memory session) ─────────────────────────────────────

    @classmethod
    def get(cls, n_events: int = 50) -> dict:
        """Last N events from the unified chronological timeline.

        Served from the in-memory session (hydrated from SQLite at startup).
        Each event has: claude_session_id, ts, type ('prompt'|'tool'|'task'|'turn'), content, ref.
        """
        cache = cls._cache
        events = [dict(e) for e in (cache[-n_events:] if n_events > 0 else [])]
        return {
            "server_session_id": SERVER_SESSION_ID,
            "started_at": STARTED_AT,
            "events": events,
        }

    @classmethod
    def reset(cls) -> None:
        """Clear both layers — test helper / manual clear."""
        cls._cache = []
        try:
            conn = cls._connect()
            try:
                conn.execute("DELETE FROM server_memory")
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            _log.warning("[server_memory] reset failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level API — thin delegators + hook-payload helpers (keep call sites stable)
# ---------------------------------------------------------------------------

def load() -> None:
    ServerMemory.load()


def record_prompt(claude_session_id: str, text: str) -> None:
    ServerMemory.record_prompt(claude_session_id, text)


def record_tool(claude_session_id: str, tool: str, args: str | None = None) -> None:
    ServerMemory.record_tool(claude_session_id, tool, args=args)


def record_task(claude_session_id: str, task_id: str, title: str) -> None:
    ServerMemory.record_task(claude_session_id, task_id, title)


def record_memories(claude_session_id: str, names: list[str]) -> None:
    ServerMemory.record_memories(claude_session_id, names)


def get_server_memory(n_events: int = 50) -> dict:
    return ServerMemory.get(n_events=n_events)


def reset() -> None:
    ServerMemory.reset()


def _title_from_response(tresp) -> str:
    """Pull title from a PostToolUse tool_response, unwrapping the MCP content envelope.

    Claude Code wraps MCP results as {"content": [{"type": "text", "text": "<json>"}]}.
    Best-effort — the envelope shape varies, so DB lookup is the authoritative source.
    """
    if not isinstance(tresp, dict):
        return ""
    if isinstance(tresp.get("content"), list) and tresp["content"]:
        import json
        try:
            tresp = json.loads(tresp["content"][0].get("text", "") or "{}")
        except Exception:
            return ""
    return tresp.get("title", "") if isinstance(tresp, dict) else ""


# _title_for_task was removed here. It read the title straight out of
# proj_tasks.db, which this repo no longer owns — task-framework does, and
# nothing here depends on it. The one caller already fell back to
# _title_from_response when the lookup came up empty, so the degraded path is
# not new code: it is the path that already ran whenever the task was absent.
# A title is a display convenience, and losing one must never be more than that.


_ARGS_MAX = 300  # truncation limit for tool_input JSON in server memory

_NATIVE_FILE_TOOLS = {"Read", "Edit", "Write"}

def record_tool_from_hook(body: dict) -> None:
    """Record a tool call from a raw PostToolUse hook payload.

    MCP tools: recorded with compact JSON args.
    Read/Edit/Write: recorded with file_path as args.
    Other native tools (Bash, etc.) are skipped — name-only, too noisy.
    tasks__set_active is skipped here — it's handled as a 'task' event by record_task_from_hook.
    """
    tool_name = body.get("tool_name", "")
    tin = body.get("tool_input") or {}

    if tool_name in _NATIVE_FILE_TOOLS:
        path = tin.get("file_path", "")
        record_tool(body.get("session_id", ""), tool_name, args=path or None)
        return

    if tool_name == "Bash":
        cmd = tin.get("command", "")
        from hooks.gates import _GIT_COMMIT_RE, _TASK_ID_RE
        if _GIT_COMMIT_RE.search(cmd):
            task_ids = _TASK_ID_RE.findall(cmd)
            args = ",".join(task_ids) if task_ids else None
            record_tool(body.get("session_id", ""), "git commit", args=args)
        return

    if not tool_name.startswith("mcp__"):
        return
    try:
        from core.tool_registry import strip_mcp_prefix
        short = strip_mcp_prefix(tool_name) or tool_name
    except Exception:
        short = tool_name
    if short == "tasks__set_active":
        return  # recorded as 'task' type by record_task_from_hook
    args: str | None = None
    if tin:
        try:
            raw = json.dumps(tin, separators=(",", ":"), ensure_ascii=False)
            args = raw if len(raw) <= _ARGS_MAX else raw[:_ARGS_MAX] + "…"
        except Exception:
            pass
    record_tool(body.get("session_id", ""), short, args=args)


def record_task_from_hook(body: dict) -> None:
    """Record a task activation from a raw PostToolUse hook payload.

    Handles the fully-qualified MCP tool_name (mcp__claude-hooks__tasks__set_active);
    title resolved authoritatively from proj_tasks.db, falling back to the response.
    """
    tool_name = body.get("tool_name", "")
    if not tool_name.startswith("mcp__"):
        return
    try:
        from core.tool_registry import strip_mcp_prefix
        short = strip_mcp_prefix(tool_name) or tool_name
    except Exception:
        short = tool_name
    if short != "tasks__set_active":
        return
    tin = body.get("tool_input") or {}
    task_id = tin.get("task_id", "") if isinstance(tin, dict) else ""
    title = _title_from_response(body.get("tool_response"))
    record_task(body.get("session_id", ""), task_id, title)
