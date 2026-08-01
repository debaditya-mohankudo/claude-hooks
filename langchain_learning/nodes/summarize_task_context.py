"""SummarizeTaskContextNode — compress active task context via BareClaudeAgent before injection.

Runs once per task activation (first turn only — gated on task_context having at
most one prior turn) to bound the added latency from the `claude -p` subprocess
call (~5s startup). A one-time 3s delay is applied before invoking the summarizer
on that single gated turn. Falls back to raw context (empty summary) on timeout
or error, same as before.

Tags: task-context, summarize, subagent, compression, context-injection, first-turn-gated
"""
from __future__ import annotations

import subprocess
import textwrap
import threading
import time
from datetime import date
from pathlib import Path

from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from langchain_learning.subagent import BareClaudeAgent
from src.logger import get_logger

_VAULT_ROOT = Path.home() / "workspace" / "claude_documents"
_TASK_CONTEXTS_DIR = _VAULT_ROOT / "TaskContexts"

# Vault RAG lives in the local-mac MCP project — index via decoupled subprocess
# (claude-hooks deliberately does not import claude_for_mac_local). Only attempted
# when the vault RAG TurboVec index already exists.
_LOCAL_MAC_DIR = Path.home() / "workspace" / "claude_for_mac_local"
_VAULT_RAG_TVIM = (
    Path.home()
    / "Library/Mobile Documents/com~apple~CloudDocs/Databases/vault_rag.tvim"
)

_log = get_logger(__name__)

_THRESHOLD_CHARS = 800
_TIMEOUT_SECONDS = 20
_PRE_INVOKE_DELAY_SECONDS = 3  # one-time, only on the single first-turn-gated invocation

_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a context compressor for a coding assistant.
    Given prior task history, related code, and related past work, produce a concise
    bullet-point summary (max 200 words):
    - What has been done so far
    - Current state / where things stand
    - Key files and modules involved
    - Relevant patterns from related prior work
    Be dense — no filler, no repetition, no preamble.
""").strip()


def _build_raw_context(state: SessionState) -> str:
    parts: list[str] = []

    task_context = state.get("task_context") or []
    if task_context:
        lines = ["## Task history"]
        unique_sids = {ev.get("session_id", "") for ev in task_context}
        multi = len(unique_sids) > 1
        for ev in task_context:
            turn = ev.get("turn", "?")
            summary = ev.get("summary", "").strip()
            tools = ev.get("tools", "").strip()
            sid = (ev.get("session_id") or "")[:8]
            line = f"- [{sid}] turn {turn}" if multi else f"- turn {turn}"
            if summary:
                line += f": {summary}"
            if tools:
                line += f" [{tools}]"
            lines.append(line)
        parts.append("\n".join(lines))

    rag_chunks = state.get("task_rag_chunks") or []
    if rag_chunks:
        lines = ["## Relevant code"]
        for c in rag_chunks:
            name = c.get("name", "")
            mod = c.get("module", "?")
            file = c.get("file", "")
            line_no = c.get("line", "")
            label = f"`{name}`" if name else f"`{mod}`"
            loc = f"{file}:{line_no}" if line_no else file
            lines.append(f"- {label} — {loc}")
        parts.append("\n".join(lines))

    related_tasks = state.get("related_tasks") or []
    if related_tasks:
        lines = ["## Related past tasks"]
        for t in related_tasks:
            lines.append(f"- {t['id']}: {t['title']}")
            if t.get("body_snippet"):
                lines.append(f"  {t['body_snippet']}")
        parts.append("\n".join(lines))

    related_commits = state.get("related_commits") or []
    if related_commits:
        lines = ["## Related commits"]
        for c in related_commits:
            commit = c.get("commit_hash", "?")
            file = c.get("file", "")
            score = c.get("score", 0)
            lines.append(f"- `{commit}` {file} [{score:.3f}]")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _save_to_vault(task_id: str, task_title: str, session_id: str, summary: str) -> None:
    """Write summary to vault TaskContexts/<task-id>/<date>_<session[:8]>.md and index it — fire-and-forget."""
    # Never pollute the real vault from tests — guard by session_id naming convention.
    if (session_id or "").startswith(("test-", "pytest-", "api-test-")):
        _log.info("[summarize_task_context] test session %s — skipping vault write", session_id)
        return

    def _write() -> None:
        try:
            # Don't create the vault from scratch — if it isn't there, skip gracefully.
            if not _VAULT_ROOT.exists():
                _log.warning("[summarize_task_context] vault root %s missing — skipping save", _VAULT_ROOT)
                return
            task_dir = _TASK_CONTEXTS_DIR / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            sid_short = (session_id or "unknown")[:8]
            path = task_dir / f"{date.today().isoformat()}_{sid_short}.md"
            if path.exists():
                return  # once per session per task
            safe_title = task_title.replace("/", "-").replace("\\", "-")[:60]
            content = (
                f"---\n"
                f"task_id: {task_id}\n"
                f"task_title: \"{safe_title}\"\n"
                f"date: {date.today().isoformat()}\n"
                f"session: {sid_short}\n"
                f"tags: [task-context, summary]\n"
                f"---\n\n"
                f"{summary}\n"
            )
            path.write_text(content, encoding="utf-8")
            relative = str(path.relative_to(_VAULT_ROOT))
            _log.info("[summarize_task_context] saved vault %s", relative)
            _index_into_vault_rag(relative)
        except Exception as exc:
            _log.warning("[summarize_task_context] vault write failed: %s", exc)

    threading.Thread(target=_write, daemon=True).start()


def _index_into_vault_rag(relative_path: str) -> None:
    """Best-effort: index the saved file into vault RAG if the index exists.

    Decoupled subprocess into the local-mac MCP project — claude-hooks does not
    import claude_for_mac_local. No-op (just-save) when the vault RAG index is
    absent or the project/subprocess is unavailable.
    """
    if not _VAULT_RAG_TVIM.exists():
        _log.info("[summarize_task_context] no vault RAG index — saved only, not indexed")
        return
    try:
        code = (
            "from src.tools.vault_rag import handle_index_file;"
            f"print(handle_index_file({relative_path!r}))"
        )
        proc = subprocess.run(
            ["uv", "run", "--directory", str(_LOCAL_MAC_DIR), "python", "-c", code],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            _log.info("[summarize_task_context] vault RAG indexed %s — %s",
                      relative_path, proc.stdout.strip())
        else:
            _log.warning("[summarize_task_context] vault RAG index failed rc=%d: %s",
                         proc.returncode, proc.stderr.strip()[:200])
    except Exception as exc:
        _log.warning("[summarize_task_context] vault RAG index error: %s", exc)


class SummarizeTaskContextNode:
    """Compress task_context + related_* + rag_chunks into a single tight summary via BareClaudeAgent.

    Skipped when no active task, not the first turn since activation, or total raw
    context < 800 chars. Falls back to empty string (raw lists used) on timeout or
    subprocess error.

    Tags: summarize-task-context, subagent, compression, haiku, bare-agent, first-turn-gated
    """

    def __call__(self, state: SessionState) -> dict:
        entry("summarize_task_context", state)

        if not state.get("active_task_id"):
            return {"task_context_summary": ""}

        # First-turn gate: task_context holds prior turns for this task (oldest-first,
        # across sessions). More than one entry means this isn't the first turn since
        # activation — skip to bound latency to a single occurrence per task.
        if len(state.get("task_context") or []) > 1:
            return {"task_context_summary": ""}

        raw = _build_raw_context(state)
        _log.info("[summarize_task_context] raw context length=%d chars", len(raw))
        if len(raw) < _THRESHOLD_CHARS:
            _log.info("[summarize_task_context] raw=%d chars < threshold — skipping", len(raw))
            return {"task_context_summary": ""}

        _log.info("[summarize_task_context] raw=%d chars — invoking BareClaudeAgent", len(raw))

        prompt = f"Summarize the following task context:\n\n{raw}"

        try:
            time.sleep(_PRE_INVOKE_DELAY_SECONDS)

            agent = BareClaudeAgent(system_prompt=_SYSTEM_PROMPT, safe_mode=True, no_tools=True)

            result: list[str] = []
            error: list[Exception] = []

            def _run() -> None:
                try:
                    result.append(agent.invoke(prompt))
                except Exception as exc:
                    error.append(exc)

            start = time.monotonic()
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=_TIMEOUT_SECONDS)
            elapsed = time.monotonic() - start

            if t.is_alive():
                _log.warning("[summarize_task_context] timeout after %ds (elapsed=%.2fs) — falling back",
                             _TIMEOUT_SECONDS, elapsed)
                return {"task_context_summary": ""}
            if error:
                _log.warning("[summarize_task_context] error after %.2fs: %s — falling back", elapsed, error[0])
                return {"task_context_summary": ""}

            summary = result[0].strip()
            _log.info("[summarize_task_context] summary=%d chars in %.2fs", len(summary), elapsed)
            _save_to_vault(
                task_id=state.get("active_task_id", ""),
                task_title=state.get("active_task_title", ""),
                session_id=state.get("session_id", ""),
                summary=summary,
            )
            return {"task_context_summary": summary}

        except Exception as exc:
            _log.warning("[summarize_task_context] error: %s — falling back", exc)
            return {"task_context_summary": ""}
