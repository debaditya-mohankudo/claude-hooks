---
tags: architecture overview, claude-hooks, hook server, MCP tools, LangGraph, session graph, memory system, gate framework, observability, FastAPI, uvicorn, system overview, components
---
# claude-hooks Architecture

> This document describes the system as built — the decisions made, why they were made, and the constraints that shaped the design. Task tracking and the concept tools are not part of it: they live in the sibling [task-framework](https://github.com/debaditya-mohankudo/Lite-Task-Framework) project, which this repo calls (fail-open) but never the reverse.

---

## Overview

`claude-hooks` is a Python system that intercepts Claude Code hook events and runs a **LangGraph StateGraph pipeline** in response. Its responsibilities are:

1. **Memory injection** — score and inject relevant memories from `MEMORY.sqlite` into every prompt (BM25 over tags plus slug words; skill-tagged memories on a `Skill` call)
2. **Commit traceability gating** — deny a `git commit` that carries no `task:<id>` reference
3. **Tool usage tracking** — accumulate latency and keyword signals per MCP tool in `tool_hints.sqlite`
4. **MCP bridge** — any tool result carrying `__hook__` is injected back into the session
5. **Observability** — structured logs in `claude_hooks.sqlite` and a rolling cross-session event log (`server_memory.sqlite`)

---

## Subsystems

- **HookServer** — FastAPI server (`hooks/server.py`, port 8766), `hooks/dispatcher.py` event handlers, the gates in `hooks/gates.py`, and the session memory timeline. Routes: `/hook/{UserPromptSubmit,PreToolUse,PostToolUse,Stop,SessionStart,SessionEnd}` plus `/health` and `/session*` read endpoints. Owns the checkpointer lifecycle.
- **LangGraphPipeline** — `StateGraph(SessionState)` in `langchain_learning/session_graph.py`, 8 registered nodes, checkpointed by `MemorySaver` (`SqliteSaver` was retired after two corruption incidents, task:b3964f85).
- **MCPTools** — `mcp_server.py` + `src/dispatcher.py`: 7 domains, 30 actions (`hooks`, `memory`, `code_rag`, `diff_rag`, `think`, `scratch`, `vault_map`). `DOMAIN_MAP` in `src/dispatcher.py` is the authoritative list.
- **Stores** — `MEMORY.sqlite` (memories, BM25), `tool_hints.sqlite`, `claude_hooks.sqlite` (logs), `server_memory.sqlite` (event log), and the TurboVec code/diff RAG indexes. `concept_store/concepts.json` holds per-module architectural facts.
- **Foundation** — config (`src/config.py`), DB schema (`src/db/schema.py`), logging. Cross-cutting, not a peer subsystem.

## The graph

Ground truth: `langchain_learning/session_graph.py:build_session_graph()`. One graph, routed on `event_type`:

| Event | Path |
| --- | --- |
| `UserPromptSubmit` | `load_turn` → `load_memories` → `set_prompt_id` → END |
| `PreToolUse` | `gate_check` → END |
| `PostToolUse` | `log_tool_usage` → (`mcp_hook_bridge` if the result has `__hook__`) → END |
| `Stop` / unknown | `noop` → `play_sound` → END |

After the graph returns, the dispatcher assembles `additionalContext` (`## Turn state`, user context, `## Injected memories`), trims it to the budget, and the response flows back through server → client → Claude Code.

The task pipeline that used to sit in this graph (`load_active_task`, `load_task_code`, `load_related_commits`, `load_related_tasks`, `load_task_history`, `summarize_task_context`, `log_task_events`, `activate_task`, `deactivate_task`) and the tool-scoring and domain-detection nodes (`score_tools`, `cwd_domain_detect`) are gone. Tombstone comments in `session_graph.py` name the task and reason for each; read them before re-adding any of these. The backfill slot (`backfill_memory_files.py`) is unwired but kept on disk — see [Pluggable Nodes](pluggable_nodes.md).

## Gates

`hooks/gates.py` keeps two gates, both on the commit path and neither touching a task store: `GitCommitGate` (Bash — regexes the command, or a file passed via `-F`, for `task:<id>`) and `GitCommitMcpGate` (`git__commit` — requires a non-empty `task_id` param). The `imessage__send` / `mail__delete` gates moved to LocalMacMCP; the task-lifecycle gates were removed when task storage moved to task-framework.

## Requirements traceability

| Requirement | Source | Satisfied by |
| --- | --- | --- |
| Commit ↔ task traceability | concept: `gates-commit-traceability` | `HookServer` |
| Additive-only migrations | concept: `db-schema-ddl-and-migrations` invariant | `Foundation` |
| Checkpoint non-persistence | [Development Workflow](dev-workflow.md) | `LangGraphPipeline` |
| Memory scoring global batch limit | concept: `memory-scoring-global-batch-limit` | `Stores` |

The enforced model lives in `models/*.sysml` and is checked by `tests/test_models.py`; verify a row against `concept_store/concepts.json` before relying on it.

---

## Design Principles

- **Hooks orchestrate; MCP servers own domain logic.** Project databases stay inside MCP servers — hooks never reach across that boundary.
- **All safety decisions are deterministic and explainable.** Gate checks are rule-based, not probabilistic.
- **Fail open.** A server that is down, a gate that errors, a parse that fails — none may block the user's tool call. `client.py` falls back to `dispatcher.py` in subprocess mode.
- **Session state is the source of truth.** `SessionState` fields in the LangGraph checkpoint carry all cross-hook context — no DB-as-IPC.
- **Modular graph nodes that can evolve independently.** Each node is a callable class registered in `nodes/registry.py`; adding behavior means adding a node, not editing existing ones.

---

## Design Constraints

- Low-latency execution on every hook — every millisecond is user-perceived latency
- Persistent session state across prompts without relying on Claude's in-context memory (in-process only: checkpoints do not survive a server restart)
- No direct access to project databases — hooks only touch their own DBs
- Deterministic gate evaluation — no heuristics that can false-positive on normal prompts

---

## System Diagram

```mermaid
flowchart TD
    CC[Claude Code] -->|Hook event| FS[FastAPI Server\nport 8766]

    FS --> UPS[UserPromptSubmit]
    FS --> PTU_pre[PreToolUse]
    FS --> PTU_post[PostToolUse]
    FS --> STOP[Stop]

    subgraph UPS Pipeline
        UPS --> LT[load_turn]
        LT --> LM[load_memories\nBM25]
        LM --> SP[set_prompt_id]
    end

    subgraph PreToolUse Pipeline
        PTU_pre --> GC[gate_check\nGitCommitGate / GitCommitMcpGate]
        GC -->|allow| ALLOW[200 proceed]
        GC -->|deny| BLOCK[200 block + reason]
    end

    subgraph PostToolUse Pipeline
        PTU_post --> LU[log_tool_usage\nlatency + keywords]
        LU -->|__hook__ in result| BR[mcp_hook_bridge]
    end

    STOP --> NOOP[noop → play_sound]

    SP -->|"## Injected memories"| CC

    LM -.->|BM25 scoring| MEMDB[(MEMORY.sqlite)]
    LU -.->|usage counts| THDB[(tool_hints.sqlite)]
    FS -.->|structured logs| LOGDB[(claude_hooks.sqlite)]
```

---

## Sections

- [Setup Guide](setup.md) — Getting claude-hooks running from scratch; database creation, hook registration, env vars
- [Pluggable Nodes](pluggable_nodes.md) — the backfill extension slot
- [Development Workflow](dev-workflow.md) · [Testing](testing.md) · [Skills](skills.md)
