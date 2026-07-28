---
tags: architecture overview, claude-hooks, hook server, MCP tools, LangGraph, session graph, memory system, gate framework, task framework, observability, FastAPI, uvicorn, system overview, components, SysML, formal model
---
# claude-hooks Architecture

> This document describes the system as built — the decisions made, why they were made, and the constraints that shaped the design. The structural/behavioral claims below are backed by a validated SysML v2 model in `docs/models/` (source of truth for structure/state/requirements/sequence — this page explains it, it doesn't restate it informally).

---

## Overview

`claude-hooks` is a Python system that intercepts all four Claude Code hook events and runs a **LangGraph StateGraph pipeline** in response. Its responsibilities are:

1. **Memory injection** — score and inject relevant memories from `MEMORY.sqlite` into every prompt
2. **Tool hint surfacing** — retrieve relevant MCP tools based on prompt intent and domain
3. **Anti-hallucination gating** — hard-block irreversible MCP tool calls unless a prerequisite tool actually ran this prompt
4. **Tool usage tracking** — accumulate latency and keyword signals per MCP tool for future retrieval
5. **Task tracking** — inject persistent work context (history, code chunks, memories) for the active task

---

## Formal Model (SysML v2)

Validated via `jupyter console --kernel=sysml` (the SysML v2 Pilot Implementation's kernel — see the MBSE learning epic that produced these, task:7af08b6b). Each `.sysml` file below parses and validates with zero errors against the real SysML v2 metamodel — not diagrams-as-illustration, a checked model.

### Subsystems (Block Definition Diagram equivalent)

Five subsystems, each composing a shared `Foundation` (config, DB schema, logging — cross-cutting, not a peer subsystem), composed into a top-level `System` part: `HookServer` (FastAPI server, event dispatcher, 3 gates, session memory timeline — owns the LangGraph checkpointer lifecycle), `MCPTools` (FastMCP dispatcher, 9 domains / 56 actions), `TaskGraph` (Jira-style issue tracking backed by `proj_tasks.db`), `MemoryConceptRAGStores` (four distinct persistent stores: memory/`MEMORY.sqlite`/BM25 — per-turn UPS scoring, `feedback`/`user`/non-repo-domain facts only since task:850ddd65; repo_memory/`repo_memory/memories.json` — per-repo committed store for `project`/`reference` facts, lifecycle-scoped only (task-activation/grooming/introspection), never per-turn scored; concept/`concepts.json`; RAG/code_rag+diff_rag on a shared TurboVec core), and `LangGraphPipeline` (`StateGraph(SessionState)`, ~28 nodes, `MemorySaver` checkpointer after a prior `SqliteSaver` corrupted).

Full source with all `doc` comments and provenance: [`docs/models/claude_hooks_system.sysml`](models/claude_hooks_system.sysml), [`docs/models/foundation.sysml`](models/foundation.sysml).

### Task lifecycle (state machine)

Transcribed directly from `src/tools/tasks.py`'s `_VALID_STATUSES`/`_TRANSITIONS`/`is_valid_transition` — not inferred. New tasks always start `openState`; `doneState`/`abandonedState` are terminal; any non-terminal state can transition to `abandonedState` (special-cased in code rather than listed per-state, modeled here as explicit transitions for clarity). Not modeled: parent auto-close-on-all-subtasks-done is a cross-task side effect (`handle_finish`), not a same-task transition.

Full source: [`docs/models/task_lifecycle.sysml`](models/task_lifecycle.sysml).

### Requirements traceability

Six requirements, each `satisfy`'d by a subsystem part, sourced from a mix of this document's own prose and `concept_store/concepts.json` invariants (see the file for full sourcing citations per requirement — every one cites its origin, none invented):

| Requirement | Source | Satisfied by |
| --- | --- | --- |
| `CommitTaskTraceabilityRequirement` | concepts: `gates-commit-traceability`, `commit-task-traceability-capture` | `hookServer`, `taskGraph` |
| `AdditiveOnlyMigrationRequirement` | concept: `db-schema-ddl-and-migrations` invariant | `foundation` |
| `LogReadViaMCPOnlyRequirement` | this doc's "Observability" section | `mcpTools` |
| `CheckpointNonPersistenceRequirement` | this doc's "Development Workflow" section | `pipeline` |
| `MemoryScoringPerDomainRequirement` | concept: `memory-scoring-per-domain-batch-limit` | `stores` |
| `JiraHierarchyRequirement` | concept: `gates-jira-hierarchy` | `taskGraph`, `hookServer` |

Full source: [`docs/models/requirements.sysml`](models/requirements.sysml).

### UserPromptSubmit sequence

SysML v2's textual notation has no distinct sequence-diagram keyword — an `action def` with explicit `first`/`then` successions is the closest fit. Ground truth: `hooks/dispatcher.py:_handle_user_prompt_submit()` and `langchain_learning/session_graph.py:build_session_graph()`. Eight ordered actions: client forwards the payload → server routes to the dispatcher → dispatcher reads existing checkpoint state → the StateGraph runs (`load_turn` → task loaders → fan-out `cwd_domain_detect`/`load_memories`/`score_tools` → `set_prompt_id` → `log_task_events`) → dispatcher adds `vault_context` → trims to the token/char budget → assembles `additionalSystemPrompt` → response flows back through server → client → Claude Code.

Full source: [`docs/models/user_prompt_submit_flow.sysml`](models/user_prompt_submit_flow.sysml).

**A prior version of this section named `hooks/memory_loader_lc.py` as an "LCEL pipeline."** That file does not exist anywhere in this repo — it belongs to a separate, global `~/.claude/` memory system unrelated to claude-hooks. The graph above (a LangGraph `StateGraph`, not LCEL — no LCEL usage exists anywhere in this repo) is the real mechanism.

**Reading the models yourself:** these are plain text — readable without tooling for the structure/names/comments — but full parse/semantic validation requires the SysML v2 Pilot Implementation's Jupyter kernel (`jupyter console --kernel=sysml`; setup notes in the MBSE epic, task:7af08b6b) or a SysML v2-capable IDE (Eclipse Papyrus).

---

## Design Principles

- **Hooks orchestrate; MCP servers own domain logic.** Project databases stay inside MCP servers — hooks never reach across that boundary.
- **All safety decisions are deterministic and explainable.** Gate checks are rule-based, not probabilistic.
- **Session state is the source of truth.** `SessionState` fields in the LangGraph checkpoint carry all cross-hook context — no DB-as-IPC.
- **Modular graph nodes that can evolve independently.** Each node is a callable class; adding behavior means adding a node, not editing existing ones.

---

## Design Constraints

- Low-latency execution on every hook — every millisecond is user-perceived latency
- Persistent session state across prompts without relying on Claude's in-context memory
- No direct access to project databases — hooks only touch their own DBs
- Deterministic gate evaluation — no heuristics that can false-positive on normal prompts
- Modular graph nodes that can evolve independently without coupling

---

## Extensibility

The architecture is designed to support:

- Additional gate policies (a new internal `Gate` class, or just a `~/.claude/gate_rules.yaml` entry for external tools — no code change)
- New memory retrieval strategies (swap `CombinationSignalRetriever` via Protocol)
- Multiple MCP servers and domains
- Richer task graphs and subtask hierarchies
- Improved retrieval algorithms (BM25 → hybrid or vector)
- Additional observability pipelines

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
        UPS --> ST[set_prompt_id\nload_turn]
        ST --> PAR[Parallel branch]
        PAR --> LM[load_memories\nCombinationSignalRetriever]
        PAR --> LT[load_related_tasks\ndiff RAG + code RAG]
        PAR --> SC[score_tools\nKeywordOverlapScorer]
        PAR --> CD[cwd_domain_detect]
        LM & LT & SC & CD --> SP[build additionalSystemPrompt]
        SP --> LE[log_task_events]
    end

    subgraph PreToolUse Pipeline
        PTU_pre --> GC[gate_check\nDefaultGatePolicy]
        GC -->|allow| ALLOW[200 proceed]
        GC -->|deny| BLOCK[200 block + reason]
    end

    subgraph PostToolUse Pipeline
        PTU_post --> LU[log_tool_usage\nlatency + keywords]
    end

    SP -->|## Injected memories\n## Suggested tools\n## Task history| CC

    LM -.->|BM25 scoring| MEMDB[(MEMORY.sqlite)]
    SC -.->|keyword overlap| THDB[(tool_hints.sqlite)]
    LT -.->|task graph| TASKDB[(proj_tasks.db)]
    LU -.->|update hints| THDB
    FS -.->|structured logs| LOGDB[(claude_hooks.sqlite)]
```

---

## Sections

- [New Repo Onboarding](new_repo_onboarding.md) — How to register a new project into `cwd_domains.json` and seed memories
- [Setup Guide](setup.md) — Getting claude-hooks running from scratch; database creation, hook registration, env vars

**Caveat on staleness:** unlike `concept_store/concepts.json` (auto drift-checked on every Edit/Write), the SysML models in `docs/models/` have no drift detection — they're a snapshot as of task:7af08b6b's epic, not automatically kept in sync with code changes. Treat them as "validated at time of writing," and re-derive if this section's subsystem/requirement/transition claims look stale against current code.
