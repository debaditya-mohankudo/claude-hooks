"""SessionState TypedDict — shared across session_graph and all nodes."""
from __future__ import annotations

from typing import TypedDict


class SessionState(TypedDict):
    # --- routing ---
    event_type: str          # "user_prompt_submit" | "pre_tool_use" | "post_tool_use" | "stop"

    # --- common ---
    prompt: str
    cwd: str
    session_id: str
    turn: int

    # --- UserPromptSubmit outputs ---
    memories: list[dict]
    keywords: list[str]
    active_task_domain: str          # domain tag of the active task (e.g. "claude-hooks"); emitted by ActivateTaskNode for downstream nodes
    task_files: list[str]            # file paths from the active task's Files: section; emitted by ActivateTaskNode, consumed by backfill nodes
    backfill_count: int              # number of memory records backfilled this activation; written by BackfillNodeProtocol implementors
    stop_alert_sent: bool            # True once NoopNode has seen the first Stop event of the current turn; reset by run_session, set by NoopNode
    sound_played: bool               # True once PlaySoundNode has fired the completion chime for the current turn; reset by run_session, set by PlaySoundNode

    # --- stop chain ---
    current_state: str               # "prompt" | "stop"

    # --- prompt tracking ---
    prompt_id: str                            # UUID generated each UserPromptSubmit; shared across hook invocations via checkpoint
    # prompt_tools, session_prompt_ids, session_tools and session_prompt_texts
    # were removed (task:84dbcf14). They grew for the whole session —
    # session_tools held every tool_input, Write bodies included — and every
    # PostToolUse copied them into a new checkpoint blob, so per-session memory
    # was O(n²). Their only reader was GateContext's history helpers, which no
    # gate called. A gate that needs cross-call history should add a bounded
    # field for exactly what it reads, not restore the full log.

    # --- PreToolUse / PostToolUse inputs ---
    tool_name: str
    tool_input: dict

    # --- PreToolUse outputs ---
    gate_denied: bool
    gate_reason: str

    # --- PostToolUse inputs ---
    duration_ms: float
    tool_result: dict                # tool_response from PostToolUse hook input
    # tool_use_id: str  # available in hook input but not consumed by any node

    # --- PostToolUse outputs ---
    pending_hook_output: dict        # set by nodes to return additionalContext etc. to the hook response; cleared after each PTU turn

