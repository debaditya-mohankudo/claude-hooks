---
description: 'Start or resume a task using the claude-hooks task graph. Creates a task, activates it for the session, and tracks work against it.'
mode: 'agent'
---

You are operating in task-framework mode. This ports the `task-framework` Claude Code skill for Copilot Chat — same `tasks__*` MCP tools (server: `claude-hooks`), same lifecycle. Read carefully.

## What the task framework does

Every task has a lifecycle tracked in `proj_tasks.db`:

```
tasks__create_scaffolded  →  task is open
tasks__set_active          →  task becomes wip; session_id bound in checkpoint
  (each turn)               →  task history injected into system prompt, IF the
                                UserPromptSubmit hook fires for this chat turn
tasks__finish               →  explicit close with reason
```

**Injection does not happen automatically for Copilot.** Copilot Chat does not fire this repo's native `UserPromptSubmit`/`PreToolUse`/`PostToolUse`/`Stop` hooks the way Claude Code does — `chat.useClaudeHooks` does not bridge that gap by itself. Use the bridge script (`hooks/copilot_client.py`) to forward events to the same hook server explicitly, once per turn where you want context refreshed:

```bash
python3 hooks/copilot_client.py prompt --prompt "<the user's message this turn>" --session-id "<session_id>"
python3 hooks/copilot_client.py pre-tool  --tool-name "<tool>" --session-id "<session_id>" --tool-args '<json>'
python3 hooks/copilot_client.py post-tool --tool-name "<tool>" --session-id "<session_id>" --tool-args '<json>'
```

Each call prints the server's JSON response, including `additionalSystemPrompt` when relevant — read it directly rather than assuming it landed in your context automatically. If the server is unreachable, the bridge fails open (prints a warning, returns `{}`) — treat that as "no injected context this turn," not an error to retry.

If you skip the bridge (e.g. quick one-off task management), pull context manually instead:

```
mcp__claude-hooks__tasks__history(task_id="<id>")
mcp__claude-hooks__tasks__neighbors(task_id="<id>")
mcp__claude-hooks__tasks__get(task_id="<id>")
```

## Getting the session_id

Call the MCP tool directly — there is no reliable "Turn state" prompt block to read from in Copilot:

```
mcp__claude-hooks__hooks__session_id()
```

Returns `{session_id, turn}`. If it returns `{error}`, no checkpoint exists yet for this session (hook hasn't fired) — retry once, and if it still fails, tell the user activation/injection won't work this turn and fall back to tracking via task body updates only (see Rules).

## Steps when invoked with a task description

### 0. Assess decomposition

If the task has 2-3 clearly distinct phases workable sequentially:
- Check the concept store if one exists (`concept_store/concepts.json` — use `mcp__taskfw__concept__list` / `concept__get`; this repo has no `concept__*` tools of its own, task:756c14db) and let subtask boundaries follow documented module boundaries where they exist, rather than cutting across them.
- Propose the subtask list to the user (one line each, in sequence) and get confirmation before creating.
- Create a parent task first, then each subtask with `parent_id=<parent_task_id>` (tags them `parent:<id>`, enables hierarchy + auto-close of parent when all subtasks finish).
- Activate the first subtask; work sequentially.

Otherwise, skip straight to creating one task. Don't force a split.

### 1. Create the task

```
mcp__claude-hooks__tasks__create_scaffolded(
  title="...",
  task_type="feature",   # or "research", "bug", etc.
  sections={...},
  cwd="<repo absolute path>"   # or domain="<domain>" for non-dev/research tasks
)
```

**Title quality:** the title is embedded for semantic neighbor search — encode *what + where + why* with concrete keywords.
- Good: `"Add memories column to task_events for per-turn injection logging"`
- Bad: `"fix gate"`, `"run tests"`

**Checklist format:** for any task with 3+ discrete file/step targets, write `Resolution:` (or `Notes:`) as a markdown checklist, not prose:

```
Resolution:
- [ ] src/tools/tasks.py — remove X
- [ ] hooks/gates.py — remove Y
```

Tick items with `- [x]` via `tasks__update(task_id=..., body=...)` as each step completes.

### 2. Activate it for this session

**Not optional.** Activation is the only thing that can unlock per-turn context injection (subject to the caveat above).

```
mcp__claude-hooks__tasks__set_active(task_id="<task_id>", session_id="<from hooks__session_id>")
```

### 3. Confirm to the user

```
Task task:<id> active — <title>
Tracking this session. Say "task:<id> done" when finished.
```

### 4. Work on the task normally

Loop: understand → think → implement → validate → reflect. Stay in scope; validate assumptions early; finish decisively rather than polishing indefinitely.

- `mcp__claude-hooks__tasks__list()` — see open/wip tasks (`limit=N` if you need more than 50)
- `mcp__claude-hooks__tasks__history(task_id=...)` — inspect logged turn events
- `mcp__claude-hooks__tasks__update(task_id=..., body=...)` — append notes / tick checklist items mid-task

**Finding code while working:**
```
mcp__claude-hooks__code_rag__smart_search(query="<symbol or concept>", repo="<abs path>")
mcp__claude-hooks__code_rag__index_files(files=["relative/path.py"], repo="<abs path>")
```

### 5. Commit before closing

No `/gc` equivalent exists yet for Copilot in this repo — commit directly, but append `task:<id>` to the commit body yourself (the same convention `/gc` follows) so the commit maps back to the task:

```
git commit -m "$(cat <<'EOF'
<summary>

task:<id>
EOF
)"
```

Order: **implement → commit per subtask → close parent task → push.**
Push manually after the parent task is closed, not before.

### 6. Closing the task

Preferred — say it in your message to the user: `task:<id> done`

Explicit:
```
mcp__claude-hooks__tasks__finish(task_id="<id>", session_id="<session_id>", reason="<what was accomplished>")
```

## Steps when invoked without a task description

`mcp__claude-hooks__tasks__list()` — show open/wip tasks, ask which to activate or whether to create a new one.

## Rules

- **Create and activate a task before any code change.** No exceptions, even for one-liners.
- **Use checklist format in `Resolution:` for tasks with 3+ discrete steps.** Update with `- [x]` as each step completes.
- **One active task per session.** Call `tasks__clear_active` first if one exists.
- **Never guess the session_id** — always get it from `hooks__session_id`.
- Injection is opt-in via `hooks/copilot_client.py`, not automatic. If you skip it, still create/track the task via body updates — just tell the user context isn't being injected this session.
- Mark tasks `done` promptly. Stale `wip` tasks accumulate stale memories.
- Commit before closing, with `task:<id>` in the commit body.
- Push after the parent task closes, not before.
