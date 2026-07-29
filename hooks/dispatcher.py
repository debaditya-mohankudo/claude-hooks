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

# Token budget for the 4 task-activation context categories (memories, related_tasks,
# related_commits, task_rag_chunks) combined. related_tasks/related_commits/task_rag_chunks
# are already capped at top-3 and pre-sorted by relevance, so `memories` — the only
# uncapped category — is the one trimmed when over budget.
_CONTEXT_TOKEN_BUDGET = 4000

# task_body is injected raw with no upstream cap (a task's body can be arbitrarily
# long, e.g. a large epic scaffold) — hard-truncate at render time.
_TASK_BODY_CHAR_CAP = 3000


def _enforce_context_budget(ctx: dict) -> None:
    """Trim ctx["memories"] (lowest-scored last, since the list is pre-sorted
    descending by score) until the combined context fits _CONTEXT_TOKEN_BUDGET
    tokens, or the list is empty. Mutates ctx in place. task_memories/
    related_tasks/related_commits/task_rag_chunks are counted toward the
    budget but never trimmed themselves — they're already small/capped, so
    memories is the only source with a meaningful relevance ordering to
    evict from.
    """
    from src.tools.tokens import count_tokens

    def _combined_tokens() -> int:
        return count_tokens("".join(
            m.get("body", "")
            for m in ctx.get("memories", []) + ctx.get("task_memories", [])
        )) + count_tokens("".join(
            t.get("body_snippet", "") for t in ctx.get("related_tasks", [])
        )) + count_tokens("".join(
            c.get("snippet", "") for c in ctx.get("related_commits", [])
        )) + count_tokens("".join(
            c.get("name", "") + c.get("module", "") for c in ctx.get("task_rag_chunks", [])
        ))

    memories = ctx.get("memories", [])
    dropped = 0
    while memories and _combined_tokens() > _CONTEXT_TOKEN_BUDGET:
        memories.pop()
        dropped += 1
    if dropped:
        log.warning(
            "UPS context budget exceeded — dropped %d lowest-scored memories to fit %d-token budget",
            dropped, _CONTEXT_TOKEN_BUDGET,
        )


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
    if vault_ctx:
        lines.append("## Soul context")
        if "user" in vault_ctx:
            lines.append("### Identity")
            lines.append(vault_ctx["user"])
            lines.append("")
        if "soul" in vault_ctx:
            lines.append("### Soul")
            lines.append(vault_ctx["soul"])
            lines.append("")
        if "memory" in vault_ctx:
            lines.append("### Memory")
            lines.append(vault_ctx["memory"])
            lines.append("")

    if ctx.get("cwd_unmapped"):
        lines.append("## New project detected")
        lines.append(
            f"This session's cwd (`{ctx.get('cwd', '')}`) doesn't match any entry in "
            "cwd_domains.json (~/Library/Mobile Documents/com~apple~CloudDocs/Databases/cwd_domains.json). "
            "Ask the user if they'd like to add a domain entry for this repo — it enables "
            "domain-weighted memory recall for tasks here. If they confirm, add a "
            '`"<cwd substring>": "<domain-slug>"` entry to the JSON directly (no code change needed).'
        )
        lines.append("")

    if ctx["domains"]:
        lines.append(f"# Active domains: {', '.join(ctx['domains'])}")
        lines.append("")

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

    if ctx.get("active_task_id"):
        title = ctx.get("active_task_title", "")
        lines.append(f"## Active task: task:{ctx['active_task_id']}" + (f" — {title}" if title else ""))
        parent_id    = ctx.get("active_parent_task_id", "")
        parent_title = ctx.get("active_parent_task_title", "")
        if parent_id:
            lines.append(f"epic: task:{parent_id}" + (f" — {parent_title}" if parent_title else ""))
        # Rendered verbatim, unlike task_body below — deliberately not subject to
        # _TASK_BODY_CHAR_CAP truncation or _enforce_context_budget eviction, since
        # the whole point is a byte-identical north star every turn the task is active.
        contract = (ctx.get("execution_contract") or "").strip()
        if contract:
            lines.append("### Execution contract")
            lines.append(contract)
            lines.append("")
        body = (ctx.get("task_body") or "").strip()
        if body:
            if len(body) > _TASK_BODY_CHAR_CAP:
                body = body[:_TASK_BODY_CHAR_CAP] + "\n...[truncated]"
            lines.append(body)
        lines.append("")

    if ctx.get("mid_task_decisions"):
        lines.append("## Task decisions")
        for decision in ctx["mid_task_decisions"]:
            lines.append(f"- {decision}")
        lines.append("")

    if ctx.get("task_memories"):
        lines.append("## Task memories")
        for mem in ctx["task_memories"]:
            name   = mem.get("name", "?")
            domain = mem.get("domain", "")
            body   = mem.get("body", "").strip()
            lines.append(f"### {name} [{domain}]")
            if body:
                lines.append(body)
            lines.append("")


    if ctx.get("task_context_summary"):
        lines.append("## Task context")
        lines.append(ctx["task_context_summary"])
        lines.append("")
    else:
        if ctx.get("task_context"):
            lines.append("## Task history")
            task_ctx     = ctx["task_context"]
            unique_sids  = {ev.get("session_id", "") for ev in task_ctx}
            multi_session = len(unique_sids) > 1
            for ev in task_ctx:
                turn    = ev.get("turn", "?")
                summary = ev.get("summary", "").strip()
                tools   = ev.get("tools", "").strip()
                sid     = (ev.get("session_id") or "")[:8]
                line = f"- [{sid}] turn {turn}" if multi_session else f"- turn {turn}"
                if summary:
                    line += f": {summary}"
                if tools:
                    line += f" [{tools}]"
                lines.append(line)
            lines.append("")

        if ctx.get("task_rag_chunks"):
            lines.append("## Relevant code")
            for c in ctx["task_rag_chunks"]:
                name = c.get("name", "")
                mod  = c.get("module", "?")
                file = c.get("file", "")
                line = c.get("line", "")
                label = f"`{name}`" if name else f"`{mod}`"
                loc = f"{file}:{line}" if line else file
                lines.append(f"- {label} — {loc}")
            lines.append("")

        if ctx.get("related_tasks"):
            lines.append("## Related past tasks")
            for t in ctx["related_tasks"]:
                lines.append(f"- {t['id']}: {t['title']}")
                if t.get("body_snippet"):
                    lines.append(f"  {t['body_snippet']}")
            lines.append("")

        if ctx.get("related_commits"):
            lines.append("## Related commits")
            for c in ctx["related_commits"]:
                commit = c.get("commit_hash", "?")
                file   = c.get("file", "")
                score  = c.get("score", 0)
                lines.append(f"- `{commit}` {file} [{score:.3f}]")
            lines.append("")

        if ctx.get("active_review"):
            rev = ctx["active_review"]
            template = rev.get("template", "")
            items = rev.get("items", [])
            if items:
                lines.append(f"## Active review checklist ({template})")
                for item in items:
                    status = item.get("status", "pending")
                    marker = "[x]" if status == "pass" else "[-]" if status == "fail" else "[ ]"
                    label = item.get("label", "")
                    kind = item.get("type", "")
                    lines.append(f"- {marker} {label} [{kind}]")
                lines.append("")

    return "\n".join(lines).strip()


from hooks.paths import VAULT_ROOT as _VAULT_ROOT
_LIFE_OS_FILES = {
    "soul":   _VAULT_ROOT / "LIFE_OS" / "soul.md",
    "user":   _VAULT_ROOT / "LIFE_OS" / "Debaditya.md",
    "memory": _VAULT_ROOT / "LIFE_OS" / "memory.md",
}


def _load_vault_context() -> dict[str, str]:
    """Read LIFE_OS md files for always-on identity/memory context."""
    result = {}
    for key, path in _LIFE_OS_FILES.items():
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                result[key] = text
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("vault_context: failed to read %s: %s", path, exc)
    return result


def _handle_user_prompt_submit(hook_input: dict) -> dict | None:
    cwd        = hook_input.get("cwd") or os.environ.get("CLAUDE_CWD") or os.getcwd()
    prompt     = _extract_prompt(hook_input)
    session_id = _get_claude_session_id(hook_input)

    # Read active_task from checkpoint before invoking the graph — needed for replay harness
    # to reconstruct task-aware inputs (related_tasks, rag_chunks, task_history).
    from langchain_learning.session_graph import get_session_graph, _config
    try:
        _saved = get_session_graph().get_state(_config(session_id))
        _active_task = (_saved.values.get("active_task_id") or "") if _saved and _saved.values else ""
    except Exception:
        _active_task = ""

    log.info("UPS enter: session=%s cwd=%s prompt_len=%d active_task=%s",
             session_id[:8], Path(cwd).name, len(prompt), _active_task[:8] if _active_task else "")

    if not prompt:
        log.info("UPS skip: empty prompt")
        return None

    t0 = time.monotonic()
    from langchain_learning.session_graph import run_session
    ctx = run_session(prompt=prompt, session_id=session_id, cwd=cwd)
    elapsed_ms = (time.monotonic() - t0) * 1000

    ctx["vault_context"] = _load_vault_context()
    _enforce_context_budget(ctx)
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

    task_history_chars = sum(
        len(ev.get("summary", "")) + len(ev.get("tools", ""))
        for ev in ctx.get("task_context", [])
    )
    # Char counts of the task-activation context categories, as a token-count proxy —
    # mirrors task_history_chars/prompt_chars above. Lets us watch which category is
    # dominating context size over time via the sqlite hook logs.
    memories_chars = sum(
        len(m.get("name", "")) + len(m.get("domain", "")) + len(m.get("body", ""))
        for m in ctx.get("memories", []) + ctx.get("task_memories", [])
    )
    related_tasks_chars = sum(
        len(t.get("title", "")) + len(t.get("body_snippet", ""))
        for t in ctx.get("related_tasks", [])
    )
    related_commits_chars = sum(
        len(c.get("commit_hash", "")) + len(c.get("file", "")) + len(c.get("snippet", ""))
        for c in ctx.get("related_commits", [])
    )
    rag_chunks_chars = sum(
        len(c.get("name", "")) + len(c.get("module", "")) + len(c.get("file", ""))
        for c in ctx.get("task_rag_chunks", [])
    )
    from src.tools.tokens import count_tokens
    memories_tokens       = count_tokens("".join(m.get("body", "") for m in ctx.get("memories", []) + ctx.get("task_memories", [])))
    related_tasks_tokens  = count_tokens("".join(t.get("body_snippet", "") for t in ctx.get("related_tasks", [])))
    related_commits_tokens = count_tokens("".join(c.get("snippet", "") for c in ctx.get("related_commits", [])))
    rag_chunks_tokens     = count_tokens("".join(c.get("name", "") + c.get("module", "") for c in ctx.get("task_rag_chunks", [])))
    prompt_tokens         = count_tokens(system_prompt)
    log.info(
        "UPS done: session=%s elapsed_ms=%.0f domains=%s memories=%d tools=%d "
        "active_task=%s task_turns=%d task_history_chars=%d rag_chunks=%s related=%s commits=%s "
        "ctx_chars(memories=%d related_tasks=%d related_commits=%d rag_chunks=%d) "
        "ctx_tokens(memories=%d related_tasks=%d related_commits=%d rag_chunks=%d) "
        "prompt_chars=%d prompt_tokens=%d",
        session_id[:8], elapsed_ms,
        ctx.get("domains", []), len(ctx.get("memories", [])), len(ctx.get("tool_hints", [])),
        ctx.get("active_task_id", ""),
        len(ctx.get("task_context", [])), task_history_chars,
        [c.get("module", "?").split(".")[-1] for c in ctx.get("task_rag_chunks", [])],
        [t["id"] for t in ctx.get("related_tasks", [])],
        [c.get("commit_hash", "?") for c in ctx.get("related_commits", [])],
        memories_chars, related_tasks_chars, related_commits_chars, rag_chunks_chars,
        memories_tokens, related_tasks_tokens, related_commits_tokens, rag_chunks_tokens,
        len(system_prompt), prompt_tokens,
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

def _record_bash_commit(hook_input: dict, tool_input: dict, session_id: str) -> None:
    """After a Bash `git commit ... task:<id>` call, look up HEAD and record the mapping.

    Best-effort: the GitCommitGate already denies commits lacking task:<id> pre-emptively,
    so this only needs to capture the SHA — failures here never block the tool response.
    """
    from hooks.gates import _GIT_COMMIT_RE, _TASK_ID_RE

    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not _GIT_COMMIT_RE.search(command):
        return
    matches = _TASK_ID_RE.findall(command)
    if not matches:
        return
    # Last match, not first — a commit body legitimately mentioning another
    # task in prose (e.g. "task:X marked abandoned, pointing here" while
    # reverting/superseding it) must not shadow this commit's own closing
    # task:<id> tag, which by convention appears last (task:a7ddc132).
    task_id = matches[-1].split(":", 1)[1]
    cwd = hook_input.get("cwd") or os.environ.get("CLAUDE_CWD") or os.getcwd()

    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            log.warning("_record_bash_commit: rev-parse failed cwd=%s stderr=%s", cwd, result.stderr.strip())
            return
        commit_hash = result.stdout.strip()
    except Exception as exc:
        log.warning("_record_bash_commit: rev-parse raised: %s", exc)
        return

    try:
        from src.tools.tasks import record_commit
        outcome = record_commit(task_id=task_id, commit_hash=commit_hash, repo_path=cwd)
        log.info("_record_bash_commit: session=%s task=%s commit=%s outcome=%s",
                  session_id[:8], task_id, commit_hash[:12], outcome)
    except Exception as exc:
        log.warning("_record_bash_commit: record_commit raised: %s", exc)


def _record_mcp_commit(tool_input: dict, tool_response: dict, session_id: str, cwd: str) -> None:
    """After a successful git__commit MCP call (any server), record the SHA it returns.

    handle_commit (claude_for_mac_local/src/tools/git.py) adds "commit_sha" to its
    response on success — mirrors _record_bash_commit but reads the SHA straight
    from the tool result instead of shelling out to rev-parse again.
    """
    if not isinstance(tool_response, dict) or not tool_response.get("ok"):
        return
    commit_hash = tool_response.get("commit_sha", "")
    task_id = (tool_input.get("task_id") or "").strip() if isinstance(tool_input, dict) else ""
    if task_id.startswith("task:"):
        task_id = task_id[len("task:"):]
    if not commit_hash or not task_id:
        return
    try:
        from src.tools.tasks import record_commit
        outcome = record_commit(task_id=task_id, commit_hash=commit_hash, repo_path=cwd)
        log.info("_record_mcp_commit: session=%s task=%s commit=%s outcome=%s",
                  session_id[:8], task_id, commit_hash[:12], outcome)
    except Exception as exc:
        log.warning("_record_mcp_commit: record_commit raised: %s", exc)


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

    if tool_name == "Bash":
        _record_bash_commit(hook_input, tool_input, session_id)

    if not tool_name or not tool_name.startswith("mcp__"):
        log.info("PTU skip: non-MCP tool=%s", tool_name)
        return None

    short_name = strip_mcp_prefix(tool_name) or tool_name
    if short_name == "git__commit":
        cwd = hook_input.get("cwd") or os.environ.get("CLAUDE_CWD") or os.getcwd()
        _record_mcp_commit(tool_input, tool_response, session_id, cwd)
    if short_name.startswith("memory__"):
        log.info("PTU skip: memory tool=%s", short_name)
        return None

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
    return hook_output or None


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------

_FAIL_CLOSED_TOOLS = {"imessage__send", "mail__compose"}

# Required body sections per workflow type, sourced from the repo's task_templates/.
# The template files (task_templates/<kind>.md) are the single source of truth — the
# gate parses them at import so the scaffolds, the create tool, and this check can
# never drift. _FALLBACK is used only if the templates dir is missing/unparseable.
_TASK_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "task_templates"
_TYPE_LINE_RE = re.compile(r"^Type:\s*(\w+)", re.MULTILINE)
_SECTION_LINE_RE = re.compile(r"^([A-Z][A-Za-z ]*):$", re.MULTILINE)  # a label alone on its line

_FALLBACK_TASK_BODY_SECTIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "feature": (("Task:", "Resolution:", "Motivation:", "Files:"), "Type: feature\n\nTask:\n...\n\nResolution:\n...\n\nMotivation:\n...\n\nFiles:\n..."),
    "bug": (("Task:", "Resolution:", "Cause:", "Files:"), "Type: bug\n\nTask:\n...\n\nResolution:\n...\n\nCause:\n...\n\nFiles:\n..."),
    "research": (("Task:", "Finding:", "Context:", "Files:"), "Type: research\n\nTask:\n...\n\nFinding:\n...\n\nContext:\n...\n\nFiles:\n(leave blank)"),
    "misc": (("Task:", "Resolution:", "Notes:", "Files:"), "Type: misc\n\nTask:\n...\n\nResolution:\n...\n\nNotes:\n...\n\nFiles:\n..."),
    "epic": (("Task:", "Resolution:", "Notes:", "Files:"), "Type: epic\n\nTask:\n...\n\nResolution:\n...\n\nNotes:\n...\n\nFiles:\n..."),
}


def _load_task_body_sections() -> dict[str, tuple[tuple[str, ...], str]]:
    """Parse task_templates/*.md → {kind: (required_section_labels, full_template_text)}.

    Each template's leading 'Type: <kind>' names the workflow kind; every label
    alone on its own line (e.g. 'Task:', 'Resolution:') is a required section.
    """
    out: dict[str, tuple[tuple[str, ...], str]] = {}
    try:
        for md in sorted(_TASK_TEMPLATES_DIR.glob("*.md")):
            if md.name.lower() == "readme.md":
                continue
            text = md.read_text(encoding="utf-8")
            tm = _TYPE_LINE_RE.search(text)
            if not tm:
                continue
            sections = tuple(f"{s}:" for s in _SECTION_LINE_RE.findall(text))
            if sections:
                out[tm.group(1).lower()] = (sections, text.strip())
    except Exception as exc:
        log.warning("_load_task_body_sections failed, falling back to defaults: %s", exc)
    return out


_TASK_BODY_SECTIONS = _load_task_body_sections() or _FALLBACK_TASK_BODY_SECTIONS
_TASK_BODY_VALID_TYPES = ", ".join(_TASK_BODY_SECTIONS)


def _check_task_body_format(tool_input: dict) -> dict | None:
    """Deny tasks__create if body is missing required sections.

    Only enforced for claude-hooks domain tasks — other project domains have
    different conventions and should not be gated by this template check.

    Body workflow type is detected from a leading 'Type: <value>' line for
    backwards compatibility, but issue_type is now a separate param.
    Denies if Type is missing/unknown or required sections are absent.
    """
    domain = (tool_input.get("domain") or "").strip().lower()
    if domain and domain != "claude-hooks":
        return None

    body = (tool_input.get("body") or "").strip()
    if not body:
        # Empty body is handle_create's documented auto-fill trigger (src/tools/tasks.py:267)
        # — only deny here if the resolved task_type has no template to fill from.
        auto_task_type = (tool_input.get("task_type") or "misc").strip().lower()
        if auto_task_type not in _TASK_BODY_SECTIONS:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Unknown task_type '{auto_task_type}' for auto-fill. "
                        f"Valid types: {_TASK_BODY_VALID_TYPES}. See task_templates/<type>.md in the repo."
                    ),
                }
            }
        return None
    m = re.search(r"^Type:\s*(\w+)", body, re.MULTILINE)
    if not m:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"tasks__create body must start with 'Type: <type>'. "
                    f"Valid types: {_TASK_BODY_VALID_TYPES}. See task_templates/<type>.md in the repo."
                ),
            }
        }
    task_type = m.group(1).lower()
    if task_type not in _TASK_BODY_SECTIONS:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Unknown task type '{task_type}'. "
                    f"Valid types: {_TASK_BODY_VALID_TYPES}. See task_templates/<type>.md in the repo."
                ),
            }
        }
    sections, fmt = _TASK_BODY_SECTIONS[task_type]
    missing = [s for s in sections if s not in body]
    if missing:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"tasks__create body (type={task_type}) is missing: {', '.join(missing)}.\n\n{fmt}"
                ),
            }
        }
    return None


# Drift reflection nudge (epic f66cccbe, task aac953b7) — rather than matching edited
# files against a declared scope list (too mechanical, and most tasks never fill in
# Files: anyway), periodically surface a soft self-check: after a run of edits under
# the same active task, ask the agent to pause and consider whether the work still
# matches the task's stated intent. No file comparison, no gate — just mild, recurring
# awareness. Works for any repo/task: reads active_task_id/title/body from the session
# checkpoint (set by activate_task.py on tasks__set_active).
_DRIFT_REFLECTION_TOOLS = {"Write", "Edit", "MultiEdit"}
_DRIFT_REFLECTION_INTERVAL = 8  # edits between nudges, per (session, task)
_DRIFT_EDIT_COUNTS: dict[tuple[str, str], int] = {}  # (session_id, active_task_id) -> count


def _maybe_drift_reflection_nudge(short_name: str, tool_input: dict, session_id: str) -> dict | None:
    if short_name not in _DRIFT_REFLECTION_TOOLS or not session_id:
        return None

    from langchain_learning.session_graph import get_session_graph, _config
    try:
        state = get_session_graph().get_state(_config(session_id))
        values = state.values if state else {}
    except Exception:
        return None

    active_task_id = values.get("active_task_id") or ""
    if not active_task_id:
        return None

    key = (session_id, active_task_id)
    count = _DRIFT_EDIT_COUNTS.get(key, 0) + 1
    _DRIFT_EDIT_COUNTS[key] = count
    if count % _DRIFT_REFLECTION_INTERVAL != 0:
        return None

    title = values.get("active_task_title", "")
    label = f"task:{active_task_id}" + (f" ({title})" if title else "")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": (
                f"Quiet check-in: {count} edits made so far under {label}. Take a moment — "
                "does the work over this stretch of edits still match the task's stated intent, "
                "or has it drifted into something adjacent? No need to answer out loud or stop; "
                "just worth noticing before continuing. Accuracy matters more than speed here — "
                "prefer verifying each step over batching many changes and hoping they land."
            ),
        }
    }


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
    # names) but must reach the reminder checks below (_maybe_drift_reflection_nudge)
    # — they used to dead-end here before it could fire.
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

    if short_name == "tasks__create":
        from hooks.gates import validate_jira_hierarchy

        tc_input = hook_input.get("tool_input") or {}
        body_denied = _check_task_body_format(tc_input)
        hierarchy_error = validate_jira_hierarchy(
            tc_input.get("issue_type", ""), tc_input.get("parent_id", "")
        )
        if body_denied or hierarchy_error:
            reasons = []
            if body_denied:
                reasons.append(body_denied["hookSpecificOutput"]["permissionDecisionReason"])
            if hierarchy_error:
                reasons.append(f"Blocked: {hierarchy_error}")
            log.info("tasks__create denied: %s", "; ".join(r[:80] for r in reasons))
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "\n\n".join(reasons),
                }
            }

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
    drift_nudge = _maybe_drift_reflection_nudge(short_name, hook_input.get("tool_input") or {}, session_id)
    if drift_nudge:
        log.info("PreTU allow+drift-reflection-nudge: session=%s tool=%s", session_id[:8], short_name)
        return drift_nudge

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
