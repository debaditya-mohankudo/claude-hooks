---
tags: skills, /gc, /pause, /onboarding, /what-am-i-working-on, skill index, slash commands, git commit skill, workflow skills
---
# Claude-hooks Skills

Skills live in `skills/<name>` and are synced to `~/.claude/skills/<name>` after every change. Invoke with `/<name>` in any Claude session.

> Task-lifecycle skills (`/task-framework`, `/task-create`, `/task-grooming`, `/task-implementation`, `/task-introspection`, `/task-log-decision`) have moved to the separate [task-framework](https://github.com/debaditya-mohankudo/Lite-Task-Framework) project.

## Skills index

| Skill | Invoke | Purpose |
| --- | --- | --- |
| `/gc` | `/gc [task:<id>]` | Git commit with automatic task tagging, test run, and code graph refresh |
| `/pause` | `/pause` | Finish current action, save pending intent (to the active task, if one exists), wait for user input |
| `/onboarding` | `/onboarding` | Interactive setup guide — walks a new teammate through full claude-hooks setup step by step |
| `/what-am-i-working-on` | `/what-am-i-working-on` | Cold-start orientation — recent prompts, tool calls, and task activations across sessions |

---

## Skill details

Full step-by-step instructions live in each skill file — these are what Claude reads at runtime. The notes below summarise when and why to use each skill.

### /gc

Stages all changes, runs unit tests, commits with a derived message, and appends `task:<id>` to the commit body automatically. Also refreshes code/diff embeddings after commit. Never pushes — push is a deliberate end-of-task action.

**Gate:** `GitCommitGate` blocks any commit missing a `task:<id>` in the body; `/gc` satisfies this automatically.

→ `skills/gc/skill.md`

---

### /pause

Finishes any in-flight tool call, saves pending intent (to the active task via `mcp__taskfw__tasks__update`, if [task-framework](https://github.com/debaditya-mohankudo/Lite-Task-Framework) is installed and a task is active), then waits for user input. Task stays active; history continues when the user resumes.

→ `skills/pause/skill.md`

---

### /what-am-i-working-on

Calls `hooks__server_memory(n_events=50)` and presents the returned timeline — recent prompts, tool calls, and task activations across sessions. Use at the start of a fresh session for quick orientation.

→ `skills/what-am-i-working-on/skill.md`

---

### /onboarding

Interactive setup guide for a new teammate. Steps: OS detection → prerequisites → clone/deps → iCloud databases → hook registration → MCP server → smoke test. Goes one step at a time waiting for confirmation.

→ `skills/onboarding/skill.md` · reference: `docs/setup.md`

---

## Syncing skills to ~/.claude

After editing any skill file in `skills/`, sync it:

```bash
cp skills/<name>/skill.md ~/.claude/skills/<name>/skill.md
```

The repo is the source of truth — `~/.claude/skills/` is the deployed copy.

---

← [Architecture](ARCHITECTURE.md) · [Setup](setup.md)
