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

The dispatcher is the single orchestrator (task:31fb711d): each handler below does
its work directly and keeps what has to survive between events in the plain
per-session dict of hooks/session_store.py. There is no graph and no checkpointer.
  UserPromptSubmit  → session_store.begin_turn + LoadMemoriesNode
  PreToolUse        → hooks.gates.check
  PostToolUse       → side effects, then LogToolUsageNode
  Stop              → session_store.mark_stop + hooks/stop_effects.py
"""
import json as _json
import os
import re
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
    """Convert the turn context dict into the injected system prompt block."""
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

    user_ctx = (ctx.get("user_context") or "").strip()
    if user_ctx:
        lines.append(user_ctx)
        lines.append("")

    # The once-per-turn '## Active task' block (task:996cc8f0) was removed here
    # (task:8be768df). It was briefly superseded by a taskfw PostToolUse hook
    # (taskfw/drift_hook.py), which task:00d9483f then removed too — there is
    # now no periodic active-task reminder from either side. taskfw announces
    # the active task once at tasks__set_active and does not re-surface it, and
    # nothing here reintroduces a per-turn announcement. The push cache that
    # once fed ctx["active_task"] (hooks/session_state.set_active_task) is gone
    # too (task:173e6846) — this handler no longer populates or logs it.

    if ctx["memories"]:
        lines.append("## Injected memories")
        for mem in ctx["memories"]:
            name   = mem.get("name", "?")
            domain = mem.get("domain", "")
            body   = mem.get("body", "").strip()
            lines.append(f"### {name} [{domain}]")
            if mem.get("conflicts_with"):
                lines.append(f"> conflicts with: {', '.join(mem['conflicts_with'])}")
            if body:
                lines.append(body)
            lines.append("")

    # The '## Suggested tools' / '## Suggested tool groups' blocks are gone: they
    # ranked by usage count, not by the prompt, and missed the right `mac` area on
    # the prompts checked. find_cli resolves a task to a command on demand;
    # tool_hints.sqlite stays as its usage store (log_tool_usage still writes it).

    # Active task, execution contract, task decisions/memories/history, relevant
    # code, and related tasks/commits blocks are gone (task:882d67fa) — that
    # context is task-framework's now, read via tasks__context rather than
    # pushed into the system prompt every turn.

    return "\n".join(lines).strip()


from hooks.user_context import render_user_context


_load_memories_node = None
_log_tool_usage_node = None


def _load_memories(state: dict) -> dict:
    """LoadMemoriesNode is one process-wide instance: it carries each session's recent areas."""
    global _load_memories_node
    if _load_memories_node is None:
        from langchain_learning.nodes.load_memories import LoadMemoriesNode
        _load_memories_node = LoadMemoriesNode()
    return _load_memories_node(state)


def _log_tool_usage(state: dict) -> dict:
    global _log_tool_usage_node
    if _log_tool_usage_node is None:
        from langchain_learning.nodes.log_tool_usage import LogToolUsageNode
        _log_tool_usage_node = LogToolUsageNode()
    return _log_tool_usage_node(state)


def _run_user_prompt_turn(prompt: str, session_id: str, cwd: str) -> dict:
    """Open the turn in the session store and load its memories. Returns the turn context."""
    from hooks import session_store
    turn = session_store.begin_turn(session_id, prompt)
    loaded = _load_memories({"prompt": prompt, "session_id": session_id})
    return {**turn, **loaded, "session_id": session_id, "cwd": cwd}


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
    ctx = _run_user_prompt_turn(prompt, session_id, cwd)
    elapsed_ms = (time.monotonic() - t0) * 1000

    # Shadow run (task:444cc036): find_cli's answer for the whole prompt is logged,
    # never shown. Never raises, so it cannot cost the turn its other context.
    from src.tools import prompt_shadow
    t_shadow = time.monotonic()
    prompt_shadow.record(session_id, prompt)
    shadow_ms = (time.monotonic() - t_shadow) * 1000

    ctx["user_context"] = render_user_context(set(ctx.get("keywords") or []))
    system_prompt = _format_system_prompt(ctx)

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
        "UPS done: session=%s elapsed_ms=%.0f shadow_ms=%.0f memories=%d "
        "ctx_chars(memories=%d) ctx_tokens(memories=%d) "
        "prompt_chars=%d prompt_tokens=%d",
        session_id[:8], elapsed_ms, shadow_ms,
        len(ctx.get("memories", [])),
        memories_chars,
        memories_tokens,
        len(system_prompt), prompt_tokens,
    )

    if system_prompt:
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": system_prompt,
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
# warning (task:e849c7ad). Non-blocking additionalContext, like
# _maybe_tmux_nudge below, but for PostToolUse instead of PreToolUse.
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


def _handle_post_tool_use(hook_input: dict) -> dict | None:
    from core.tool_registry import strip_mcp_prefix

    tool_name   = hook_input.get("tool_name", "")
    session_id  = hook_input.get("session_id", "")
    duration_ms = float(hook_input.get("duration_ms", 0))
    tool_input  = hook_input.get("tool_input", {})
    tool_response = hook_input.get("tool_response") or {}

    log.info("PTU enter: session=%s tool=%s duration_ms=%.0f", session_id[:8], tool_name, duration_ms)
    log.debug("tool_response raw: %r", tool_response)
    if tool_name == "WebSearch":
        from src.tools import web_search_cache
        if web_search_cache.record_hook(session_id, tool_input, hook_input.get("tool_response")):
            log.info("PTU websearch cached: session=%s", session_id[:8])
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

    # The `mac <area> <verb>` commands this call ran, from a Bash command line or
    # from an MCP tool `mac` replaces (core/mac_command.py, task:0aa96180).
    from core.mac_command import calls_for
    mac_calls = calls_for(tool_name, tool_input)

    # find_cli hit rate (task:cf5d016d): store each lookup, link the next mac call
    # to it. Before the nudge's early return; both calls never raise. The raw
    # response is passed because MCP results arrive as a content-block list.
    from src.tools import find_cli_log
    if tool_name == find_cli_log.FIND_TOOL:
        find_cli_log.record_query(session_id, tool_input, hook_input.get("tool_response"))
    elif mac_calls:
        outcome = find_cli_log.link_calls(session_id, [c.id for c in mac_calls])
        if outcome:
            log.info("PTU find_cli link: session=%s outcome=%s", session_id[:8], outcome)
    if mac_calls:
        from src.tools import prompt_shadow
        prompt_shadow.link_calls(session_id, [c.id for c in mac_calls])

    # Before the nudge's early return, so a nudge turn never skips a vault reindex.
    # Keeps the vault SQLite index current after vault writes (src/tools/vault_index.py);
    # it never raises and records failures in the index's meta table.
    if mac_calls:
        from src.tools.vault_index import reindex_for_command
        for call in mac_calls:
            result = reindex_for_command(call.id, _vault_input(call))
            if result:
                log.info("PTU vault reindex: %s %s", call.id, result)

    # Edits `reindex_for_command` cannot see: Edit/Write on a vault note, or a Bash command
    # (sed -i, a redirect) naming one. Unchanged notes cost one stat; never raises.
    from src.tools.vault_index import reindex_for_tool
    result = reindex_for_tool(tool_name, tool_input, hook_input.get("cwd", ""))
    if result:
        log.info("PTU vault reindex (tool): %s %s", tool_name, result)

    context_nudge = _maybe_context_size_nudge(hook_input, session_id)
    if context_nudge:
        log.info("PTU context-size nudge: session=%s tool=%s", session_id[:8], tool_name)
        return context_nudge

    # Skill-tagged memories (task:85801ef4): before the non-MCP skip below.
    from langchain_learning.nodes import _skill_memories
    if tool_name == "Skill":
        return _skill_memories.inject_for_skill(
            session_id, (tool_input or {}).get("skill", "") if isinstance(tool_input, dict) else "")
    if tool_name == "Bash" and mac_calls and isinstance(tool_input, dict):
        nudge = _skill_memories.tag_nudge(session_id, [c.id for c in mac_calls], tool_input.get("command", ""))
        if nudge:
            log.info("PTU skill-tag nudge: session=%s", session_id[:8])
            return nudge

    if mac_calls:
        if all(call.area == "memory" for call in mac_calls):
            log.info("PTU skip: memory command=%s", mac_calls[0].id)
            return None
        names = [call.id for call in mac_calls]
        if tool_name == "Bash":
            tool_response = _bash_result(tool_response) if len(mac_calls) == 1 else {}
    elif tool_name.startswith("mcp__"):
        short_name = strip_mcp_prefix(tool_name) or tool_name
        if short_name.startswith("memory__"):
            log.info("PTU skip: memory tool=%s", short_name)
            return None
        names = [short_name]
    else:
        log.info("PTU skip: non-MCP tool=%s", tool_name)
        return None

    from hooks import session_store
    turn = session_store.get(session_id)
    tool_input_clean = tool_input if isinstance(tool_input, dict) else {}

    for name in names:
        t0 = time.monotonic()
        _log_tool_usage({
            "tool_name": name,
            "tool_input": tool_input_clean,
            "tool_result": tool_response,
            "session_id": session_id,
            "duration_ms": duration_ms / len(names),
            "prompt": turn["prompt"],
            "prompt_id": turn["prompt_id"],
        })
        log.info("PTU done: session=%s tool=%s elapsed_ms=%.0f", session_id[:8], name,
                 (time.monotonic() - t0) * 1000)
    return None


def _vault_input(call) -> dict:
    """A `mac vault` call's arguments as the MCP vault tools take them: path, and `to` for a move."""
    if call.params:
        return call.params
    from core.mac_command import positionals
    pos = positionals(call.args)
    out = {"path": pos[0]} if pos else {}
    if call.verb == "move" and len(pos) > 1:
        out["to"] = pos[1]
    return out


def _bash_result(tool_response: dict) -> dict:
    """A Bash `mac` call's JSON output as a dict, so hint learning sees the result, not the shell wrapper."""
    import json as _json
    try:
        parsed = _json.loads(tool_response.get("stdout") or "")
    except (ValueError, AttributeError):
        return tool_response
    return parsed if isinstance(parsed, dict) else {"result": parsed}


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------

# _FAIL_CLOSED_COMMANDS ({"mac imessage send", "mac mail compose"}, task:0aa96180)
# and the deny-on-handler-error branch in main() that read it were removed
# (task:4e56d23a). It ran only in main(), the subprocess path; production goes
# through client.py -> server.py, where a handler error is a 500 that the client
# turns into {} (allow), so it protected nothing. Nothing here gates those
# commands any more: LocalMacMCP owns the send gate (claude_for_mac_local
# src/tool_hooks.py _imessage_guard, which fails closed itself), and the `mac`
# CLI reaches it through local-mac's dispatcher. A PreToolUse handler error now
# fails open on both paths, as every other hook error does. Don't re-add a
# send-specific rule here; it belongs with the tool.

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
# there instead.
#
# task-framework then removed the ported nudge as well (task:00d9483f). A brief
# middle phase (task:8be768df / task:1c8f0815) had this file shell out to a
# taskfw-drift-hook subprocess from _handle_post_tool_use on every PostToolUse
# call, threading a per-session call counter in over stdin. All of that is gone:
# no subprocess, no _TASKFW_DRIFT_CALL_COUNT, no _merge_drift_nudge. taskfw now
# announces its active task once at tasks__set_active and never re-surfaces it.
# The /set-active-taskid push cache (hooks/session_state) that briefly let this
# repo know the active task at all is also gone (task:173e6846). Do not re-add a
# claude-hooks-side active-task nudge in any form, do not re-add the push cache,
# and do not re-add the '## Active task' UserPromptSubmit block removed alongside
# the original (see _format_system_prompt). task-framework remains the one owner
# of the active task and any reminder derived from it.


# Nudge toward tmux for Bash commands (memory: prefer-tmux-for-commands) — tmux panes
# survive across tool calls and let Claude inspect long-running/interactive output
# (servers, REPLs, watchers) without blocking the turn. Advisory only: it returns
# additionalContext and NO permissionDecision. An "allow" here would approve the
# Bash call outright and skip the user's permission rules (task:d2e7cff6), so the
# normal permission flow must decide. Fires once per session on the first
# qualifying Bash call, not on every Bash call, so it doesn't nag on one-shot commands.
#
# Hints match only at command heads (task:5d6872d4): the command line is split into
# simple commands and each one's program (and subcommand) is looked at. The
# whole-word regex this replaced (_TMUX_NUDGE_HINT_RE) still searched quoted
# strings, heredoc script text and arguments, so a JSON key or a variable named
# "watch" fired it. Bare "watch" stays, but only as the program being run.
_TMUX_NUDGE_SHOWN: set[str] = set()
_TMUX_LONG_RUNNING_PROGRAMS = {"serve", "runserver", "watch", "uvicorn", "http.server"}
_TMUX_LONG_RUNNING_SUBCOMMANDS = {
    "npm": (["run"],), "yarn": (["dev"],), "pnpm": (["dev"],),
    "flask": (["run"],), "rails": (["s"], ["server"]),
    "docker": (["compose", "up"],), "docker-compose": (["up"],),
    "django-admin": (["runserver"],), "manage.py": (["runserver"],),
}
# Words that run the command after them, so the program is further along.
_TMUX_WRAPPERS = {
    "sudo", "nohup", "time", "env", "nice", "exec", "command", "npx", "bunx",
    "if", "then", "else", "do", "while", "until", "!", "{",
}
# Programs whose "--watch" argument is text to find or print, not a mode.
_TMUX_TEXT_PROGRAMS = {"grep", "rg", "echo", "printf"}


def _shell_commands(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands, each a list of words.

    A quoted string stays one word; comments, heredoc bodies and redirect targets
    are dropped. Not a shell parser: it only has to find the program names.
    """
    commands: list[list[str]] = []
    words: list[str] = []
    heredocs: list[str] = []
    word, drop_next, i, n = None, False, 0, len(command)

    def end_word():
        nonlocal word, drop_next
        if word is not None:
            if not drop_next:
                words.append(word)
            word, drop_next = None, False

    def end_command():
        nonlocal words, drop_next
        end_word()
        if words:
            commands.append(words)
        words, drop_next = [], False

    while i < n:
        c = command[i]
        if c == "\\":
            if command[i + 1:i + 2] != "\n":
                word = (word or "") + command[i + 1:i + 2]
            i += 2
        elif c in "'\"":
            j = i + 1
            while j < n and command[j] != c:
                j += 2 if c == '"' and command[j] == "\\" else 1
            word = (word or "") + command[i + 1:j]
            i = j + 1
        elif c == "#" and word is None:
            j = command.find("\n", i)
            i = n if j < 0 else j
        elif command.startswith("<<", i) and not command.startswith("<<<", i):
            end_word()
            m = re.compile(r"<<-?[ \t]*\\?(['\"]?)([\w.-]+)\1").match(command, i)
            if m:
                heredocs.append(m.group(2))
            i = m.end() if m else i + 2
        elif c in "<>" or command.startswith("&>", i):
            if word is not None and word.isdigit():
                word = None  # the fd of "2>"
            end_word()
            while i < n and command[i] in "<>&":
                i += 1
            drop_next = True
        elif c == "\n":
            end_command()
            i += 1
            while heredocs and i < n:
                j = command.find("\n", i)
                j = n if j < 0 else j
                if command[i:j].strip() == heredocs[0]:
                    heredocs.pop(0)
                i = j + 1
        elif c in ";&|()`":
            end_command()
            i += 1
        elif c in " \t\r":
            end_word()
            i += 1
        else:
            word = (word or "") + c
            i += 1
    end_command()
    return commands


def _is_long_running(words: list[str]) -> bool:
    """Whether one simple command's program is a server, watcher or follower."""
    words = list(words)
    while words:
        name = words[0].rsplit("/", 1)[-1]
        if re.match(r"[a-z_]\w*=", words[0], re.I) or name in _TMUX_WRAPPERS:
            del words[0]
        elif name in ("uv", "poetry", "pipenv") and words[1:2] == ["run"] or (
                name == "bundle" and words[1:2] == ["exec"]):
            del words[:2]
        elif re.fullmatch(r"python[\d.]*", name) and words[1:2] == ["-m"]:
            del words[:2]
        elif re.fullmatch(r"python[\d.]*", name) and words[1:2] and words[1].endswith("manage.py"):
            del words[0]
        else:
            break
        while words and words[0].startswith("-"):
            del words[0]
    if not words:
        return False
    name, args = words[0].rsplit("/", 1)[-1], words[1:]
    if name in _TMUX_LONG_RUNNING_PROGRAMS:
        return True
    if any(args[:len(sub)] == sub for sub in _TMUX_LONG_RUNNING_SUBCOMMANDS.get(name, ())):
        return True
    if name == "tail":
        return any(a == "--follow" or re.fullmatch(r"-[a-z0-9]*f", a) for a in args)
    return "--watch" in args and name not in _TMUX_TEXT_PROGRAMS


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
    if "tmux" in command or not any(map(_is_long_running, _shell_commands(command))):
        return None
    _TMUX_NUDGE_SHOWN.add(session_id)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": _TMUX_NUDGE_TEXT,
        }
    }


def _gate_check(tool_name: str, tool_input: dict, session_id: str) -> tuple[bool, str]:
    """Run the gate registered for tool_name, if any. Returns (denied, reason).

    The prompt_id is the log tag the gate prints; a first-turn tool call that
    precedes any UserPromptSubmit gets a generated "fb-" id so the row stays traceable.
    """
    from hooks.gates import GATES, GateContext, check
    if tool_name not in GATES:
        return False, ""
    from hooks import session_store
    prompt_id = session_store.get(session_id)["prompt_id"]
    if not prompt_id:
        import hashlib
        prompt_id = "fb-" + hashlib.sha256(f"{session_id or 'anon'}-{time.time()}".encode()).hexdigest()[:12]
        log.info("PreTU gate session=%s generated fallback prompt_id=%s (no UPS yet)",
                 session_id[:8] or "?", prompt_id)
    deny, reason = check(tool_name, GateContext(tool_name=tool_name, tool_input=tool_input, prompt_id=prompt_id))
    log.info("PreTU gate: tool=%s prompt_id=%s %s", tool_name, prompt_id[:8], "DENY" if deny else "ALLOW")
    return deny, reason


def _handle_pre_tool_use(hook_input: dict) -> dict | None:
    from core.tool_registry import strip_mcp_prefix

    tool_name  = hook_input.get("tool_name", "")
    session_id = hook_input.get("session_id", "")

    _t0 = time.monotonic()
    log.info("PreTU enter: session=%s tool=%s", session_id[:8] if session_id else "?", tool_name)

    if not tool_name or not session_id:
        return None

    # Built-in tools (e.g. Bash) are gated directly by tool_name; MCP tools are stripped.
    # Edit/Write/MultiEdit have no gate entry (hooks.gates.check no-ops for unregistered tool
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
    elif tool_name == "WebSearch":
        # Serve a fresh cached result instead of searching again
        # (src/tools/web_search_cache.py). A miss falls through to the real search.
        # "deny" is the only PreToolUse decision that skips the tool and still hands
        # text back; the cache module owns both the reason and the user-facing
        # systemMessage so the two halves cannot drift (task:665c7305).
        from src.tools import web_search_cache
        cached = web_search_cache.cached_hit_output(session_id, hook_input.get("tool_input") or {})
        if cached:
            log.info("PreTU websearch cache hit: session=%s", session_id[:8])
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": cached["permissionDecisionReason"],
                },
                "systemMessage": cached["systemMessage"],
            }
        return None
    else:
        return None

    denied, reason = _gate_check(short_name, hook_input.get("tool_input") or {}, session_id)
    if denied:
        log.info("PreTU deny: session=%s tool=%s reason=%s", session_id[:8], short_name, reason[:80])
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    tmux_nudge = _maybe_tmux_nudge(short_name, hook_input.get("tool_input") or {}, session_id)
    if tmux_nudge:
        log.info("PreTU allow+tmux-nudge: session=%s tool=%s", session_id[:8], short_name)
        return tmux_nudge

    log.info("PreTU allow: session=%s tool=%s elapsed_ms=%.1f", session_id[:8], short_name, (time.monotonic() - _t0) * 1000)
    return None


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def _handle_session_end(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id", "")

    from hooks import session_store
    if not session_id:
        log.info("SessionEnd: session=? status=skipped")
        return None
    status = "evicted" if session_store.end(session_id) else "not_found"
    log.info("SessionEnd: session=%s status=%s", session_id[:8], status)
    return None


def _handle_session_start(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id", "")
    from hooks import session_store
    is_new = session_store.start(session_id) if session_id else False
    status = "new" if is_new else "resumed"
    log.info("SessionStart: session=%s status=%s", session_id[:8] if session_id else "?", status)
    return None


def _handle_stop(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id", "")
    if not session_id:
        return None

    log.info("Stop enter: session=%s", session_id[:8])
    t0 = time.monotonic()
    from hooks import session_store, stop_effects
    chime = session_store.mark_stop(session_id)
    stop_effects.log_live_sessions_if_multiple()
    if chime:
        stop_effects.play_completion_sound(session_id)
    log.info("Stop done: session=%s elapsed_ms=%.0f%s", session_id[:8], (time.monotonic() - t0) * 1000,
             " chime=1" if chime else "")
    return None


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

    try:
        hook_input = read_stdin()
        result = handler(hook_input)
        write_json_to_stdout(result if result else None)
    except Exception as e:
        log.error("%s handler failed: %s", hook_event, e)
        # No PreToolUse fail-closed branch here; see the tombstone above
        # _FAIL_CLOSED_COMMANDS's old place (task:4e56d23a).
        write_json_to_stdout(error=f"{hook_event} handler failed: {e}")
        if _lc_cfg.dev_mode:
            sys.exit(2)


if __name__ == "__main__":
    main()
