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
    domains: list[str]
    keywords: list[str]
    tool_hints: list[dict]
    active_task_id: str              # set via task_activate branch; flows through session via checkpoint
    active_task_title: str           # task title, set alongside active_task_id
    active_parent_task_id: str       # parent task id (epic), if the active task has one
    active_parent_task_title: str    # parent task title for context injection
    task_memories: list[dict]        # memories scored against task tags+title (task_activate branch)
    task_context: list[dict]         # prior turn events for active task (current session only)
    task_rag_chunks: list[dict]      # top-3 code modules from TurboVec semantic search over .code_embeddings.tvim
    task_body: str                    # body of the active task (goal, motivation, resolution) — injected into system prompt
    execution_contract: str          # fixed north-star block generated at activation; rendered verbatim every turn — exempt from context-budget eviction and task_body truncation
    task_context_summary: str         # compressed summary of task_context + related_* + rag_chunks via claude -p; replaces raw lists when present
    task_stack: list[str]            # LIFO stack of suspended task IDs; push on switch, pop to restore
    mid_task_decisions: list[str]    # explicit design decisions logged during active task (persisted in checkpoint)
    related_tasks: list[dict]        # top-3 done tasks by cosine similarity via TurboVec (.tasks_embeddings.tvim)
    related_commits: list[dict]      # top-3 diff hunks from TurboVec semantic search over .diff_embeddings.tvim
    active_review: dict              # open review child task checklist — {review_task_id, template, items: [{id, label, type, status}]}
    active_task_domain: str          # domain tag of the active task (e.g. "claude-hooks"); emitted by ActivateTaskNode for downstream nodes
    task_files: list[str]            # file paths from the active task's Files: section; emitted by ActivateTaskNode, consumed by backfill nodes
    backfill_count: int              # number of memory records backfilled this activation; written by BackfillNodeProtocol implementors
    cwd_unmapped: bool               # True this turn if cwd matched no CWD_DOMAIN_MAP entry and the reminder hasn't fired yet this session
    cwd_domain_reminder_sent: bool   # True once the unmapped-cwd reminder has been shown this session (persisted via checkpoint)
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

