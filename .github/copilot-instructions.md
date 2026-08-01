# Copilot instructions for claude-hooks

This repo is a lightweight, agent-operated task/memory framework for Claude
Code — persistent task tracking, memory injection, and structured decision
logging, implemented as Claude Code hooks + an MCP server + a LangGraph
pipeline. macOS-only (uv, Ollama, iCloud Drive).

## Big picture

- **`hooks/`** — Claude Code hook entry points (`server.py` runs a uvicorn
  server; `dispatcher.py` routes the 4 hook events — UserPromptSubmit,
  PreToolUse, PostToolUse, Stop — into the LangGraph pipeline; `gates.py`
  enforces commit/task policy, e.g. blocking `git commit` without a
  `task:<id>` reference in the message body).
- **`langchain_learning/session_graph.py`** — the core LangGraph
  `StateGraph(SessionState)`. All 4 hook events flow through **one** graph,
  conditionally routed on `event_type`. Checkpointed via `SqliteSaver`
  (`~/.claude/langgraph_checkpoints.db`), keyed by `session_id`.
  - UserPromptSubmit topology: `load_turn` → task-context fan-out
    (`load_task_history`, `load_task_code`, `load_related_tasks`,
    `load_related_commits`, run in parallel when a task is active) →
    `cwd_domain_detect ∥ load_memories ∥ score_tools` →
    `set_prompt_id → log_task_events → END`.
  - Individual nodes live in `langchain_learning/nodes/*.py`, one class per
    node, each a thin `__call__(state) -> dict` that returns a partial state
    update (LangGraph merges it).
- **`src/`** — shared library code: `config.py` (pydantic-settings, paths
  under `icloud_db_dir` — JSON configs like `cwd_domains.json`,
  `memory_scoring.json` live there, not baked into source, so they're
  editable without a redeploy), `tools/` (task tracking, memory — the logic
  behind the MCP tools), `db/` (SQLite schema/access).
- **`mcp_server.py`** — registers MCP tools (`tasks__*`, `memory__*`,
  `code_rag__*`, `diff_rag__*`, `hooks__*`) that Claude Code (and any
  MCP-capable agent) calls directly mid-session.
- **`concept_store/concepts.json`** — curated architectural facts per
  module (description, invariants, contracts, evidence). Drift-checked
  automatically after edits in Claude Code; not enforced for Copilot, but
  worth reading before touching a module listed there — it documents
  invariants that aren't obvious from the code alone.

## Conventions that matter

- **Task-tag commit gate**: commits in this repo are expected to reference
  a task id in the body (`task:<id>`, optionally `epic:<id>`). This is
  enforced by a Claude Code hook (`hooks/gates.py`), not by git itself —
  Copilot-authored commits won't be blocked, but match the convention when
  writing commit messages by hand.
- **cwd-first, default-repo-fallback pattern**: any node/tool resolving a
  repo-scoped resource (RAG index, diff index) should try the caller's
  `cwd` first and only fall back to a hardcoded default repo if nothing
  exists there. See `langchain_learning/nodes/load_task_code.py` and
  `load_related_commits.py` for the reference implementation — don't
  hardcode a single repo path in new nodes.
- **mtime-cached JSON config pattern**: config that should be hot-editable
  without a redeploy (e.g. `cwd_domains.json`) is loaded via a small
  `_load_x(path)` helper that re-reads only when the file's mtime changes,
  with an in-code default as a fallback. See `src/config.py`.
## Running things

```bash
# Unit tests (fast, no server needed)
uv run python -m pytest tests/ -q -m "not integration"

# Full suite (needs the live hook server — see below)
uv run python -m pytest tests/ -q
```

- The **live hook server** runs from a separate worktree
  (`~/workspace/claude-hooks-test`, port 8766) with its own checkpoint DB —
  editing this repo does not affect it directly; changes are deployed via
  `scripts/deploy.sh` (dev → test → main).
- Don't assume `git rev-parse HEAD` reflects the running server's code —
  the server tracks whichever worktree/branch was last deployed.

## When editing

- Check `concept_store/concepts.json` for the module you're touching before
  changing behavior — it records invariants (e.g. "always cwd-first before
  falling back to default repo") that aren't visible from a single file.
- Prefer extending an existing node/tool over adding a new one-off script;
  the LangGraph fan-out and MCP tool registries are the two integration
  points almost everything goes through.
- Config that varies per-user/per-repo belongs in `icloud_db_dir` as JSON,
  not hardcoded in `src/config.py`.
