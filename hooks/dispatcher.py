#!/usr/bin/env python3
"""
Single entry point for all Claude Code hook events.

Usage:
    dispatcher.py <hook_event>

    hook_event: UserPromptSubmit | PostToolUse | PreToolUse | Stop

Each handler is responsible for one Claude Code hook type. All share:
  - sys.path setup
  - read_stdin / write_json_to_stdout
  - dev_mode sys.exit(2) on error

Session graph call graph:
  UserPromptSubmit  → run_session()
  PostToolUse       → run_post_tool()
  PreToolUse        → run_gate()
  Stop              → run_stop()
"""
import json as _json
import os
import re
import subprocess as _subprocess
import sys
import time
from pathlib import Path

from hooks.paths import PROJECT_ROOT as _PROJECT_ROOT
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from langchain_learning.config import config as _lc_cfg
from src.logger import setup
from utils import read_stdin, write_json_to_stdout

log = setup("dispatcher")

# ---------------------------------------------------------------------------
# Shared extractors
# ---------------------------------------------------------------------------

def _get_claude_session_id(hook_input: dict) -> str:
    """Extract the Claude Code session UUID — the authoritative session identity."""
    return hook_input.get("session_id", "")


def _extract_prompt(hook_input: dict) -> str:
    prompt = hook_input.get("prompt", "")
    if not prompt:
        msg     = hook_input.get("message") or {}
        content = msg.get("content", "")
        if isinstance(content, str):
            prompt = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    prompt += block.get("text", "")
    # Strip injected XML context tags before storing (avoids noise in tool hints)
    prompt = re.sub(r"<[a-z_]+>[^<]{0,2000}</[a-z_]+>\n?", "", prompt, flags=re.DOTALL)
    return prompt.strip()


# ---------------------------------------------------------------------------
# UserPromptSubmit
# ---------------------------------------------------------------------------

def _format_system_prompt(ctx: dict) -> str:
    """Convert SessionState dict into the injected system prompt block."""
    lines: list[str] = []

    session_id = ctx.get("session_id", "")
    prompt_id  = ctx.get("prompt_id", "")
    if session_id or prompt_id:
        lines.append("## Turn state")
        if session_id:
            lines.append(f"- session_id: {session_id}")
        if prompt_id:
            lines.append(f"- prompt_id: {prompt_id}")
        lines.append("")

    vault_ctx = ctx.get("vault_context") or {}
    if "dev_personality" in vault_ctx:
        lines.append("## Dev personality")
        lines.append(vault_ctx["dev_personality"])
        lines.append("")

    # The once-per-turn '## Active task' block (task:996cc8f0) was removed here
    # (task:8be768df): taskfw's PostToolUse hook (taskfw/drift_hook.py) now
    # announces the active task on every tool call in the turn, not just at
    # turn start, making this weaker announcement redundant rather than
    # complementary — see MEMORY.md's now-superseded
    # active_task_reminder_vs_drift_nudge note. ctx["active_task"] is still
    # populated below and logged for observability; it just no longer renders
    # into the injected prompt.

    if ctx["memories"]:
        lines.append("## Injected memories")
        for mem in ctx["memories"]:
            name   = mem.get("name", "?")
            domain = mem.get("domain", "")
            body   = mem.get("body", "").strip()
            lines.append(f"### {name} [{domain}]")
            if body:
                lines.append(body)
            lines.append("")

    if ctx["tool_hints"]:
        lines.append("## Suggested tools")
        for hint in ctx["tool_hints"]:
            tool  = hint.get("tool_name", "?")
            skill = hint.get("skill", "")
            count = hint.get("count", 0)
            lines.append(f"- `{tool}` (skill={skill}, used={count}x)")
        lines.append("")

    # Active task, execution contract, task decisions/memories/history, relevant
    # code, and related tasks/commits blocks are gone (task:882d67fa) — that
    # context is task-framework's now, read via tasks__context rather than
    # pushed into the system prompt every turn.

    return "\n".join(lines).strip()


from hooks.paths import VAULT_ROOT as _VAULT_ROOT
_LIFE_OS_FILES = {
    "dev_personality": _VAULT_ROOT / "LIFE_OS" / "dev_personality.md",
    # "work": _VAULT_ROOT / "LIFE_OS" / "work.md",  # replaced by dev_personality.md (task:9bbd67dd)
}


from hooks.cache_store import get_cache as _get_cache
_vault_context_cache = _get_cache("vault_context")


def _load_vault_context() -> dict[str, str]:
    """Read LIFE_OS md files for always-on identity/memory context.

    iCloud's file provider briefly locks these files during sync, raising
    OSError [Errno 11] EDEADLK. dispatcher.py is imported once into the
    long-running hook server process (hooks/server.py, task:b3964f85), so the
    "vault_context" entry in hooks/cache_store.py's registry survives across
    hook calls without needing disk state (also readable via GET
    /cache/vault_context). Fall back to the last successfully read content
    rather than dropping the context for that turn.
    """
    result = {}
    for key, path in _LIFE_OS_FILES.items():
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                result[key] = text
                _vault_context_cache[key] = text
        except FileNotFoundError:
            pass
        except Exception as exc:
            if key in _vault_context_cache:
                log.info("vault_context: failed to read %s (%s), using cached copy", path, exc)
                result[key] = _vault_context_cache[key]
            else:
                log.warning("vault_context: failed to read %s: %s", path, exc)
    return result


def _handle_user_prompt_submit(hook_input: dict) -> dict | None:
    cwd        = hook_input.get("cwd") or os.environ.get("CLAUDE_CWD") or os.getcwd()
    prompt     = _extract_prompt(hook_input)
    session_id = _get_claude_session_id(hook_input)

    log.info("UPS enter: session=%s cwd=%s prompt_len=%d",
             session_id[:8], Path(cwd).name, len(prompt))

    if not prompt:
        log.info("UPS skip: empty prompt")
        return None

    t0 = time.monotonic()
    from langchain_learning.session_graph import run_session
    ctx = run_session(prompt=prompt, session_id=session_id, cwd=cwd)
    elapsed_ms = (time.monotonic() - t0) * 1000

    ctx["vault_context"] = _load_vault_context()
    from hooks.session_state import get_active_task
    active_task = get_active_task(cwd)
    ctx["active_task"] = active_task
    if active_task.get("task_id"):
        log.info(
            "UPS active_task: session=%s workspace=%s task_id=%s title=%.60r",
            session_id[:8], cwd, active_task["task_id"], active_task.get("title", ""),
        )
    else:
        log.debug("UPS active_task: session=%s workspace=%s none pushed", session_id[:8], cwd)
    system_prompt = _format_system_prompt(ctx)

    # One-shot node output for this UPS turn (e.g. LogTaskEventsNode's
    # introspection nudge after a "task:<id> done" auto-close). run_session()
    # already cleared the checkpoint copy, so this renders exactly once.
    # Gated on hookEventName so a stale PostToolUse payload is never
    # misattributed to the UPS response.
    _pho = (ctx.get("pending_hook_output") or {}).get("hookSpecificOutput") or {}
    if _pho.get("hookEventName") == "UserPromptSubmit" and _pho.get("additionalContext"):
        nudge = _pho["additionalContext"]
        system_prompt = f"{system_prompt}\n\n{nudge}" if system_prompt else nudge
        log.info("UPS one-shot hook output appended: %.80s", nudge)

    # Char/token counts of the task-activation context categories (task_history,
    # rag_chunks, related_tasks, related_commits) are gone with the nodes that
    # produced them (task:882d67fa) — task-framework owns that context now and
    # it is never pushed into the system prompt from here.
    memories_chars = sum(
        len(m.get("name", "")) + len(m.get("domain", "")) + len(m.get("body", ""))
        for m in ctx.get("memories", [])
    )
    from src.tools.tokens import count_tokens
    memories_tokens = count_tokens("".join(m.get("body", "") for m in ctx.get("memories", [])))
    prompt_tokens   = count_tokens(system_prompt)
    log.info(
        "UPS done: session=%s elapsed_ms=%.0f memories=%d tools=%d "
        "ctx_chars(memories=%d) ctx_tokens(memories=%d) "
        "prompt_chars=%d prompt_tokens=%d active_task=%s",
        session_id[:8], elapsed_ms,
        len(ctx.get("memories", [])), len(ctx.get("tool_hints", [])),
        memories_chars,
        memories_tokens,
        len(system_prompt), prompt_tokens,
        active_task.get("task_id") or "none",
    )

    if system_prompt:
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalSystemPrompt": system_prompt,
            }
        }
    return None


# ---------------------------------------------------------------------------
# PostToolUse
# ---------------------------------------------------------------------------

# Context-size nudge — reads the transcript's real API usage (input_tokens +
# cache_creation_input_tokens + cache_read_input_tokens from the most recent
# assistant turn) rather than approximating with tiktoken: that figure is
# Anthropic's actual context size. tiktoken/cl100k_base (src/tools/tokens.py
# count_tokens) is only an approximation used elsewhere to size this repo's
# own injected memory payload, not the full conversation. Fires once per 50K
# band crossed (100K, 150K, 200K, ...) so it reminds periodically as context
# keeps growing instead of nagging every call or going silent after the first
# warning (task:e849c7ad). Non-blocking, same allow+additionalContext shape
# as _maybe_tmux_nudge below, but for PostToolUse instead of PreToolUse.
_CONTEXT_NUDGE_THRESHOLD = 100_000
_CONTEXT_NUDGE_STEP = 50_000
_CONTEXT_NUDGE_SHOWN_BAND: dict[str, int] = {}


def _read_last_usage(transcript_path: str) -> int | None:
    """Total context tokens from the last assistant `usage` block in the transcript, or None."""
    import json as _json

    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 65536)
            f.seek(size - block)
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError as exc:
        log.debug("context nudge: transcript read failed: %s", exc)
        return None

    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = _json.loads(line)
        except ValueError:
            continue
        usage = (entry.get("message") or {}).get("usage")
        if not usage:
            continue
        return (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
    return None


def _maybe_context_size_nudge(hook_input: dict, session_id: str) -> dict | None:
    if not session_id:
        return None
    transcript_path = hook_input.get("transcript_path", "")
    log.debug("context nudge: transcript_path=%r", transcript_path)
    total = _read_last_usage(transcript_path)
    if total is None or total < _CONTEXT_NUDGE_THRESHOLD:
        return None
    band = total // _CONTEXT_NUDGE_STEP
    if _CONTEXT_NUDGE_SHOWN_BAND.get(session_id) == band:
        return None
    _CONTEXT_NUDGE_SHOWN_BAND[session_id] = band
    band_tokens = band * _CONTEXT_NUDGE_STEP
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"Context notice: this conversation has crossed ~{band_tokens:,} tokens "
                f"(current estimate ~{total:,}). Consider wrapping up, summarizing, or "
                "starting a fresh session soon to avoid running into context-window limits."
            ),
        }
    }


_TASKFW_DRIFT_HOOK_BIN = "/Users/debaditya/workspace/task-framework/.venv/bin/taskfw-drift-hook"

# Per-session PostToolUse call count, feeding taskfw's own every-Nth-call gate
# (task:1c8f0815). taskfw/drift_hook.py runs as a fresh subprocess per call, so it
# has no way to count calls itself — this process is the long-running one
# (dispatcher.py runs inside claude-hooks' FastAPI server, one process per
# server lifetime, not per call), so it's the only side that can cheaply keep
# an in-memory per-session counter, same pattern as _CONTEXT_NUDGE_SHOWN_BAND
# above. The counter is only ever incremented and handed to taskfw as raw
# data; the decision of whether to nudge on a given count stays inside
# taskfw's own drift_reflection_nudge, per this file's existing rule that
# task-framework owns the active-task/drift logic.
_TASKFW_DRIFT_CALL_COUNT: dict[str, int] = {}


def _maybe_taskfw_drift_nudge(hook_input: dict, cwd: str) -> dict | None:
    """Awareness nudge for taskfw's active task, sourced from taskfw itself.

    Shells out to taskfw's own CLI (taskfw/drift_hook.py, task:8be768df)
    rather than reading or reimplementing any task-framework state here —
    same fail-open subprocess pattern as GitCommitGate._head_commit_message
    (hooks/gates.py: subprocess.run(..., timeout=..., check=False) wrapped in
    a broad except), applied to a foreign binary instead of git. A daemon and
    a direct Python import were both considered and rejected earlier
    (task:8be768df's grooming) over cross-venv coupling and process
    lifecycle cost; a stateless CLI subprocess call carries neither — it's
    the same shape this file already uses for git, just pointed at a
    different binary.

    Runs before the `if not tool_name.startswith('mcp__')` early-return below
    so it fires on every tool type (Bash/Read/Write/Edit included), not just
    MCP calls — matching _maybe_context_size_nudge's placement, the only
    other nudge in this file with that requirement.

    task-framework remains the sole owner of the active-task/drift logic —
    this only invokes taskfw's own binary and passes its output through
    unchanged; it never reads taskfw's store or re-derives the nudge text.
    The one exception is the call counter (task:1c8f0815): taskfw can't hold
    that state itself (see _TASKFW_DRIFT_CALL_COUNT above), so it's computed
    here and injected into the payload as "_taskfw_drift_call_count" for
    taskfw to gate on.
    """
    session_id = hook_input.get("session_id", "")
    call_count = _TASKFW_DRIFT_CALL_COUNT.get(session_id, 0) + 1
    _TASKFW_DRIFT_CALL_COUNT[session_id] = call_count
    payload = {**hook_input, "_taskfw_drift_call_count": call_count}
    try:
        result = _subprocess.run(
            [_TASKFW_DRIFT_HOOK_BIN],
            input=_json.dumps(payload).encode(),
            capture_output=True, timeout=5, check=False,
            env={**os.environ, "TASKFW_SCOPE": cwd},
        )
        if result.returncode != 0:
            log.info(
                "taskfw drift nudge non-zero exit: session=%s call_count=%d rc=%d stderr=%r",
                session_id[:8], call_count, result.returncode, result.stderr[:500],
            )
            return None
        if not result.stdout.strip():
            # Silence is ambiguous (drift_hook.py's own docstring): no active
            # task and "throttled, not this call_count" look identical on
            # stdout, and taskfw already owns and logs both cases on its own
            # side (throttled at INFO; no-active-task deliberately at DEBUG,
            # to avoid flooding on the common case) — logging here too would
            # just be a second, less precise copy of a decision this process
            # doesn't own. See task:50aa6dc5.
            return None
        # taskfw already logs "drift nudge fired" at INFO on its own side;
        # this stays silent rather than duplicating it. See task:50aa6dc5.
        return _json.loads(result.stdout)
    except Exception as exc:
        log.info("taskfw drift nudge failed: session=%s call_count=%d error=%s", session_id[:8], call_count, exc)
        return None


def _merge_drift_nudge(hook_output: dict | None, drift_nudge: dict | None) -> dict | None:
    """Fold drift_nudge's additionalContext onto hook_output's, or return
    whichever one is present alone. Both share the same hookSpecificOutput/
    additionalContext shape (PostToolUse has no other field to collide on),
    so concatenating the two context strings is a safe merge, not a
    structural one.
    """
    if not drift_nudge:
        return hook_output
    if not hook_output:
        return drift_nudge
    drift_text = drift_nudge.get("hookSpecificOutput", {}).get("additionalContext", "")
    existing = hook_output.setdefault("hookSpecificOutput", {})
    existing["additionalContext"] = "\n\n".join(
        filter(None, [existing.get("additionalContext", ""), drift_text])
    )
    return hook_output


def _handle_post_tool_use(hook_input: dict) -> dict | None:
    from core.tool_registry import strip_mcp_prefix

    tool_name   = hook_input.get("tool_name", "")
    session_id  = hook_input.get("session_id", "")
    duration_ms = float(hook_input.get("duration_ms", 0))
    tool_input  = hook_input.get("tool_input", {})
    tool_response = hook_input.get("tool_response") or {}

    log.info("PTU enter: session=%s tool=%s duration_ms=%.0f", session_id[:8], tool_name, duration_ms)
    log.debug("tool_response raw: %r", tool_response)
    if not isinstance(tool_response, dict):
        tool_response = {"raw": str(tool_response)}

    # Claude Code wraps MCP responses: {"content": [{"type": "text", "text": "<json>"}]}
    if "content" in tool_response and isinstance(tool_response.get("content"), list):
        try:
            import json as _json
            text = tool_response["content"][0].get("text", "")
            parsed = _json.loads(text)
            if isinstance(parsed, dict):
                tool_response = parsed
        except Exception as exc:
            log.debug("tool_response content parse failed, using raw shape: %s", exc)

    context_nudge = _maybe_context_size_nudge(hook_input, session_id)
    if context_nudge:
        log.info("PTU context-size nudge: session=%s tool=%s", session_id[:8], tool_name)
        return context_nudge

    # Computed here (before the mcp__-only early-returns below) so it covers
    # every tool type, but NOT returned here — this fires on every call
    # (task:8be768df, no interval), unlike the rare _maybe_context_size_nudge
    # above, so returning early would permanently skip run_post_tool (tool-hint
    # logging, memory scoring) for as long as any task is active. Merged into
    # whichever exit point below actually fires instead; see _merge_drift_nudge.
    cwd = hook_input.get("cwd") or os.environ.get("CLAUDE_CWD") or os.getcwd()
    drift_nudge = _maybe_taskfw_drift_nudge(hook_input, cwd)
    if drift_nudge:
        log.info("PTU taskfw drift nudge: session=%s tool=%s", session_id[:8], tool_name)

    if not tool_name or not tool_name.startswith("mcp__"):
        log.info("PTU skip: non-MCP tool=%s", tool_name)
        return drift_nudge

    short_name = strip_mcp_prefix(tool_name) or tool_name
    if short_name.startswith("memory__"):
        log.info("PTU skip: memory tool=%s", short_name)
        return drift_nudge

    from langchain_learning.session_graph import run_post_tool, get_session_graph, _config
    try:
        state = get_session_graph().get_state(_config(session_id))
        prompt = (state.values.get("prompt") or "") if state and state.values else ""
    except Exception:
        prompt = ""

    tool_input_clean = tool_input if isinstance(tool_input, dict) else {}

    t0 = time.monotonic()
    hook_output = run_post_tool(
        tool_name=short_name,
        tool_input=tool_input_clean,
        tool_result=tool_response,
        session_id=session_id,
        duration_ms=duration_ms,
        prompt=prompt,
    )
    elapsed = (time.monotonic() - t0) * 1000
    if hook_output:
        log.info("PTU done: session=%s tool=%s elapsed_ms=%.0f hook_output=yes", session_id[:8], short_name, elapsed)
    else:
        log.info("PTU done: session=%s tool=%s elapsed_ms=%.0f", session_id[:8], short_name, elapsed)
    return _merge_drift_nudge(hook_output or None, drift_nudge)


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------

_FAIL_CLOSED_TOOLS = {"imessage__send", "mail__compose"}

# The task-body-template gate that lived here (_check_task_body_format and its
# supporting constants) was removed with src/tools/tasks.py (task:87ec7876).
# It existed to enforce task_templates/*.md against mcp__claude-hooks__tasks__create
# payloads — but that tool's only implementation, handle_create_scaffolded, was
# also in src/tools/tasks.py. With the handler gone, this server can no longer
# receive a tasks__create call at all, so the gate was checking a call path
# that could never fire: dead code enforcing a rule with no rule-breaker left
# to check. task_templates/ went with it (task:d6ddb40f).


# Drift reflection nudge — removed (task:f1d46386). It read active_task_id/title
# from a claude-hooks session checkpoint (set by the now-deleted activate_task.py,
# see 6633bf1) — task-framework owns the active task now, so the nudge was ported
# there instead: taskfw.dispatcher.drift_reflection_nudge.
#
# Trigger mechanism changed again (task:8be768df): it was wired onto 7 of
# taskfw's own MCP tools (tasks__update/check_item/add_decision/add_commit/
# context/get/active), which only saw taskfw's own tool calls — a stretch of
# Bash/Read/Write/Edit turns with no taskfw call never advanced it. It now
# fires from taskfw/drift_hook.py, a stateless PostToolUse hook Claude Code
# invokes directly (registered in settings.json alongside this file's own
# client.py entry), on every tool call in the session regardless of which
# server it targets.
# Do not re-add a claude-hooks-side copy of the nudge itself, and do not
# re-add the '## Active task' UserPromptSubmit block removed alongside this
# (see _format_system_prompt) — it announced the active task once per turn,
# which the every-call PostToolUse hook now makes redundant rather than
# complementary. task-framework remains the one owner of the active task and
# any reminder derived from it.


# Nudge toward tmux for Bash commands (memory: prefer-tmux-for-commands) — tmux panes
# survive across tool calls and let Claude inspect long-running/interactive output
# (servers, REPLs, watchers) without blocking the turn. Non-blocking: allow +
# additionalContext. Fires once per session on the first qualifying Bash call, not
# on every Bash call, so it doesn't nag on simple one-shot commands.
_TMUX_NUDGE_SHOWN: set[str] = set()
_TMUX_NUDGE_COMMAND_HINTS = (
    "npm run", "yarn dev", "pnpm dev", "serve", "watch", "--watch",
    "http.server", "uvicorn", "flask run", "rails s", "tail -f",
    "docker compose up", "docker-compose up",
)
_TMUX_NUDGE_TEXT = (
    "Reminder: for long-running or interactive commands (dev servers, watchers, "
    "REPLs, tailing logs), prefer running them inside a tmux session (tmux new-session, "
    "tmux send-keys, tmux capture-pane) rather than a plain foreground Bash call. tmux "
    "keeps the process alive and inspectable across tool calls instead of blocking or "
    "losing output when the call returns."
)


def _maybe_tmux_nudge(short_name: str, tool_input: dict, session_id: str) -> dict | None:
    if short_name != "Bash" or not session_id or session_id in _TMUX_NUDGE_SHOWN:
        return None
    command = (tool_input.get("command") or "").lower()
    if "tmux" in command or not any(hint in command for hint in _TMUX_NUDGE_COMMAND_HINTS):
        return None
    _TMUX_NUDGE_SHOWN.add(session_id)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": _TMUX_NUDGE_TEXT,
        }
    }


def _handle_pre_tool_use(hook_input: dict) -> dict | None:
    from core.tool_registry import strip_mcp_prefix

    tool_name  = hook_input.get("tool_name", "")
    session_id = hook_input.get("session_id", "")

    log.info("PreTU enter: session=%s tool=%s", session_id[:8] if session_id else "?", tool_name)

    if not tool_name or not session_id:
        return None

    # Built-in tools (e.g. Bash) are gated directly by tool_name; MCP tools are stripped.
    # Edit/Write/MultiEdit have no gate entry (run_gate no-ops for unregistered tool
    # names). They used to also need to reach the drift-reflection nudge below;
    # that nudge moved to task-framework (task:f1d46386), but the branch stays in
    # case a future Edit/Write/MultiEdit-specific gate or reminder needs it again.
    if tool_name == "Bash":
        short_name = "Bash"
    elif tool_name.startswith("mcp__"):
        short_name = strip_mcp_prefix(tool_name)
        if not short_name or short_name.startswith("memory__"):
            return None
    elif tool_name in ("Edit", "Write", "MultiEdit"):
        short_name = tool_name
    else:
        return None

    from langchain_learning.session_graph import run_gate
    result = run_gate(
        tool_name=short_name,
        tool_input=hook_input.get("tool_input") or {},
        session_id=session_id,
    )

    if result["gate_denied"]:
        log.info("PreTU deny: session=%s tool=%s reason=%s", session_id[:8], short_name, result["gate_reason"][:80])
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": result["gate_reason"],
            }
        }
    tmux_nudge = _maybe_tmux_nudge(short_name, hook_input.get("tool_input") or {}, session_id)
    if tmux_nudge:
        log.info("PreTU allow+tmux-nudge: session=%s tool=%s", session_id[:8], short_name)
        return tmux_nudge

    log.info("PreTU allow: session=%s tool=%s", session_id[:8], short_name)
    return None


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def _handle_session_end(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id", "")

    import langchain_learning.session_graph as sg
    # Check the already-built graph for this session's routing (test-prefixed
    # sessions route to sg._test_graph, task:b63088a1) without lazily creating
    # one just to immediately delete from it.
    is_test = session_id.startswith(sg.TEST_SESSION_PREFIX)
    graph = sg._test_graph if is_test else sg._graph
    if not session_id or not graph:
        log.info("SessionEnd: session=%s status=skipped", (session_id or "?")[:8])
        return None
    try:
        graph.checkpointer.delete_thread(session_id)
        status = "evicted"
    except Exception:
        status = "not_found"
    log.info("SessionEnd: session=%s status=%s", session_id[:8], status)
    return None


def _handle_session_start(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id", "")
    from langchain_learning.session_graph import prewarm_session
    is_new = prewarm_session(session_id)
    status = "new" if is_new else "resumed"
    log.info("SessionStart: session=%s status=%s", session_id[:8] if session_id else "?", status)
    return None


def _handle_stop(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id", "")
    if not session_id:
        return None

    log.info("Stop enter: session=%s", session_id[:8])
    t0 = time.monotonic()
    from langchain_learning.session_graph import run_stop
    hook_output = run_stop(session_id=session_id)
    log.info("Stop done: session=%s elapsed_ms=%.0f%s", session_id[:8], (time.monotonic() - t0) * 1000,
              " sound_alert=1" if hook_output else "")
    return hook_output or None


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_HANDLERS = {
    "UserPromptSubmit": _handle_user_prompt_submit,
    "PostToolUse":      _handle_post_tool_use,
    "PreToolUse":       _handle_pre_tool_use,
    "Stop":             _handle_stop,
    "SessionStart":     _handle_session_start,
    "SessionEnd":       _handle_session_end,
}


def main():
    hook_event = sys.argv[1] if len(sys.argv) > 1 else ""
    handler = _HANDLERS.get(hook_event)

    if not handler:
        log.error("Unknown hook event: %r", hook_event)
        write_json_to_stdout(error=f"Unknown hook event: {hook_event!r}")
        return

    hook_input: dict = {}
    try:
        hook_input = read_stdin()
        result = handler(hook_input)
        write_json_to_stdout(result if result else None)
    except Exception as e:
        log.error("%s handler failed: %s", hook_event, e)
        # PreToolUse fail-closed: irreversible tools must deny on any error
        if hook_event == "PreToolUse":
            from core.tool_registry import strip_mcp_prefix
            short = strip_mcp_prefix(hook_input.get("tool_name", "")) if hook_input else ""
            if short in _FAIL_CLOSED_TOOLS:
                write_json_to_stdout({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"Gate check failed (internal error) — {short} blocked for safety.",
                    }
                })
                return
        write_json_to_stdout(error=f"{hook_event} handler failed: {e}")
        if _lc_cfg.dev_mode:
            sys.exit(2)


if __name__ == "__main__":
    main()
