"""SessionState TypedDict — shared across session_graph and all nodes."""
from __future__ import annotations

from collections import OrderedDict
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
    tool_hints: list[dict]
    active_task_domain: str          # domain tag of the active task (e.g. "claude-hooks"); emitted by ActivateTaskNode for downstream nodes
    task_files: list[str]            # file paths from the active task's Files: section; emitted by ActivateTaskNode, consumed by backfill nodes
    backfill_count: int              # number of memory records backfilled this activation; written by BackfillNodeProtocol implementors
    stop_alert_sent: bool            # True once NoopNode has seen the first Stop event of the current turn; reset by run_session, set by NoopNode
    sound_played: bool               # True once PlaySoundNode has fired the completion chime for the current turn; reset by run_session, set by PlaySoundNode

    # --- stop chain ---
    current_state: str               # "prompt" | "stop"

    # --- prompt tracking ---
    prompt_id: str                            # UUID generated each UserPromptSubmit; shared across hook invocations via checkpoint
    prompt_tools: list[str]                   # tool short-names called this prompt (appended by log_tool_usage, reset by set_prompt_id)
    session_prompt_ids: list[str]             # ordered list of all prompt_ids in this session
    session_tools: OrderedDict[str, list[dict]]  # prompt_id → [{"tool": str, "tool_input": dict, "ts": float}]; used by gates for input-aware prev_tools()
    session_prompt_texts: dict[str, str]      # prompt_id → prompt text; used by gates to check name across current + prev turn

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

