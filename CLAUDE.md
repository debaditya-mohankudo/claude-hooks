# claude-hooks

## Concept Store

Architectural concepts for this repo are stored in `concept_store/concepts.json`.

**Seed (run once):**

```bash
uv run python scripts/extract_concepts.py
```

**Drift detection** runs automatically after every Edit/Write via a post-tool-use hook — prints `[concept-drift]` to stderr if a changed file's concepts diverge from the baseline. No output = no drift.

To re-seed after major refactors, delete `concept_store/concepts.json` and re-run the seed command.

**Manual reads/writes** (correcting a concept, adding a new one, checking what's stored for a module) should go through the `concept__get`/`concept__list`/`concept__upsert`/`concept__delete`/`concept__modules` MCP tools rather than hand-writing a Python script against `concept_store/store.py` — they take `repo` explicitly (required, no default), so they work the same way against this repo or any other non-Java target repo with a `concept_store/concepts.json`.

## Task Tracking

`/task-framework` is the entry point for all multi-step work — start here to create, activate, and manage a task. Use `/task-create` when creating tasks that need the full body template with motivation, files, and design decisions.

Before starting work on subtasks, run `/task-grooming epic:<id>` (or `/task-grooming task:<id>`) — activates each task, audits the body for gaps, and reports readiness. Findings are read from and written to the task's structured **document** (`document.grooming`, via `tasks__get`/`tasks__update_document`) — not appended into the body.

After closing a task, run `/task-introspection` — surfaces unlogged decisions, grades the prior grooming pass's `document.grooming.risks`, checks for stale memories, and writes its own findings to `document.introspection`.

Tasks persist across sessions, surface automatically when referenced, and build a development trail. Use TodoWrite only for ephemeral within-session sub-steps.

## Running Tests

The hook server runs from the **test worktree** (`~/workspace/claude-hooks-test`, port 8766). Dev worktree edits never affect the running server.

Run unit tests in dev at any time (fast, no server needed):

```bash
cd ~/workspace/claude-hooks-dev
uv run python -m pytest tests/ -q -m "not integration"
```

To deploy and run the full suite, use `/deploy`.

## Recent Activity / Conversation History

To see "what was I working on?" — use `/what-am-i-working-on`. It fetches recent prompts, MCP tool calls, and activated tasks as a single chronological timeline from the hook server's event log.

Returns `{error}` if the server is down — check `launchctl list | grep claude-hooks`.

## Development Workflow (git worktree)

The hook server runs from `~/workspace/claude-hooks-test` (test branch) with
**MemorySaver** (in-process, in-memory — task:b3964f85 retired SqliteSaver after
two corruption incidents). Checkpoint state does NOT survive server restarts;
`~/.claude/langgraph_checkpoints.db` is a retired file nothing writes to anymore.
Server runs on port **8766**. `/deploy` restarts it after each merge.

Develop in the isolated worktree at `~/workspace/claude-hooks-dev` (dev branch):

```bash
# 0. Merge main into dev first (task:701215e2) — catches any commits main has
#    that dev doesn't (e.g. an out-of-band direct-to-main edit) before they can
#    cause a real conflict at /deploy --ship time instead of here, where it's
#    cheap and obvious to resolve.
cd ~/workspace/claude-hooks-dev
git merge main --no-edit

# 1. Edit in dev worktree

# 2. Quick unit tests (no server needed)
uv run python -m pytest tests/ -q -m "not integration"

# 3. Commit
/gc

# 4. Deploy to test + full suite + ship to main
/deploy
```

**Key rules:**

- Edits go in `~/workspace/claude-hooks-dev` (dev branch) — never touch main or test directly
- **Merge main into dev before committing new work**, not just before `/deploy` — main can drift ahead via direct edits (it has, more than once), and catching that early avoids a conflict resolution during `/deploy --ship`
- `/gc` commits target `--repo ~/workspace/claude-hooks-dev`
- Server runs from `claude-hooks-test` — dev edits never disrupt live Claude Code hooks
- main is never touched except by `/deploy`

## Observability

All hook logs write to `claude_hooks.sqlite` in iCloud via `sqlite_log_handler.py`.

**Always use the MCP tool to read logs — never query the DB directly with sqlite3:**

```text
mcp__claude-hooks__hooks__read_logs_sqlite
```

## Memory Store
MEMORY.sqlite - Use memory__ mcp tools

## UserPromptSubmit Flow

`hooks/client.py` → `hooks/server.py` (FastAPI) → `hooks/dispatcher.py:_handle_user_prompt_submit()`,
which invokes `langchain_learning/session_graph.py:run_session()` — a LangGraph
`StateGraph`, **not** an LCEL chain (no LCEL usage exists anywhere in this repo).
Graph shape is documented in `session_graph.py`'s module docstring:
`load_turn` → `load_active_task`/`load_related_tasks` → fan-out loaders →
`summarize_task_context` → fan-out (`cwd_domain_detect`, `load_memories`,
`score_tools`) → `set_prompt_id` → `log_task_events` → END. After the graph
returns, the dispatcher (not a graph node) adds `vault_context`, enforces the
context budget, and formats `additionalSystemPrompt` via `_format_system_prompt()`.

**Do not confuse this with `hooks/memory_loader_lc.py`** — that file, described
in the user's global `~/.claude/CLAUDE.md` as an "LCEL pipeline," does not exist
in this repo (confirmed via filesystem search) and belongs to a separate, global
`~/.claude/` memory system, unrelated to this repo's architecture.
