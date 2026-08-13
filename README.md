# claude-hooks

A small FastAPI server that intercepts Claude Code's hook events — persistent memory injection, tool-use gates, and observability across sessions.

> **macOS only** — requires Claude Code, uv, Ollama, and iCloud Drive. See [setup](docs/setup.md) for prerequisites.

---

## Looking for task tracking?

Task tracking (epics/tasks/subtasks, grooming, decision logging, retrospectives) has moved to its own project: [task-framework](https://github.com/debaditya-mohankudo/Lite-Task-Framework-w-Claude-hooks). It's MCP-native and host-agnostic — install it separately and its `taskfw-mcp` server plugs into this repo's hooks the same way any other MCP server does. This repo no longer ships the `/task-framework`, `/task-create`, `/task-grooming`, `/task-implementation`, `/task-introspection`, or `/task-log-decision` skills — they live there now.

---

## New here?

```text
/onboarding
```

Run this in Claude Code after cloning the repo. It detects your OS, checks prerequisites, walks you through hooks and MCP server registration with your real paths filled in, and verifies the setup — one step at a time.

---

## Gates — hard stops before anything irreversible

Gates are the one part of this system that isn't a suggestion. They sit in `PreToolUse`, before a tool call executes, and they deny it outright if a prerequisite wasn't actually satisfied — no relying on Claude's own judgment or its in-context memory of "yeah I already checked that."

The gates for `imessage__send` and `mail__delete` have moved to [LocalMacMCP](https://github.com/debaditya-mohankudo/LocalMacMCP) — they live alongside the MCP tools they guard now, not here.

The one gate this repo still owns is the git-commit gate: it requires every commit to reference an active task, rather than trusting Claude's own claim that one exists.

```text
Bash(git commit -m "fix: ... task:27a9c6ed")
```

```text
✓ ALLOW — task:27a9c6ed found in commit message → commit created
```

Commit without a `task:<id>` reference (in the message, or in a file passed via `-F`) and it's denied before the commit is made:

```text
Blocked: git commit is missing a task:<id> reference. Add 'task:<id>' to the commit
message body (or the file passed via -F), or activate a task first with
tasks__set_active.
```

It needs database access to resolve active tasks, so it stays as a small Python class rather than a YAML-declared gate like the ones that moved to LocalMacMCP.

---

## claude-hooks, briefly

Underneath the gates is a small FastAPI server that intercepts all four Claude Code hook events (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`) and runs them through one LangGraph pipeline. It injects relevant memories and (if [task-framework](https://github.com/debaditya-mohankudo/Lite-Task-Framework-w-Claude-hooks) is installed) active-task context into every prompt, tracks which MCP tools get used so it can recommend the right one next time, and keeps all of that state durable across restarts in a single SQLite-backed checkpoint — so nothing above (gates, memory) depends on Claude's own context window to stay coherent.

**The full architecture** — components, design decisions, database inventory, the graph topology itself: [Architecture](docs/ARCHITECTURE.md).

---

## Mid-session

### Pausing gracefully

```text
/pause
```

```text
Paused. Completed: middleware updated, tests passing.

Pending (saved to task:7f1e):
- Update the logout endpoint to revoke Redis tokens
- Add token TTL config to settings.py

Waiting for your input.
```

Claude captures what's still pending and stops — no half-finished reasoning, no context dropped. If [task-framework](https://github.com/debaditya-mohankudo/Lite-Task-Framework-w-Claude-hooks) is installed and a task is active, the pending list is also saved to the task so it resurfaces next session.

---

## Skills

Skills are listed in the order you'd use them across a real session.

### Setup

| Skill         | What it does                                                                  |
| ------------- | ----------------------------------------------------------------------------- |
| `/onboarding` | Interactive setup guide — clone, prereqs, hooks, MCP server, smoke test       |

### Starting your day

| Skill                    | What it does                                                                        |
| ------------------------ | ----------------------------------------------------------------------------------- |
| `/what-am-i-working-on`  | Show recent prompts, tool calls, and activated tasks — your Monday-morning restore  |

### Mid-task

| Skill    | What it does                                                                               |
| -------- | -------------------------------------------------------------------------------------------|
| `/pause` | Finish the current action, save pending intent (to the active task, if one exists), and wait for your input |

### Git workflow

| Skill | What it does                                                                        |
| ----- | ------------------------------------------------------------------------------------ |
| `/gc` | Commit — runs pre-commit tests, embeds the active `task:<id>` in the commit message |

---

## Docs

- [Architecture](docs/ARCHITECTURE.md) — how the pipeline is structured and why
- [Setup](docs/setup.md) — installation and configuration
- [Skills](docs/skills.md) — full skill reference
