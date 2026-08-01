---
tags: skills, /gc, /task-framework, /task-create, /task-task-log-decision, /pause, /onboarding, /what-am-i-working-on, /task-introspection, /task-grooming, skill index, slash commands, git commit skill, task creation skill, decision logging, workflow skills, grooming
---
# Claude-hooks Skills

Skills live in `skills/<name>` and are synced to `~/.claude/skills/<name>` after every change. Invoke with `/<name>` in any Claude session.

## Skills index

| Skill | Invoke | Purpose |
| --- | --- | --- |
| `/gc` | `/gc [task:<id>]` | Git commit with automatic task tagging, test run, and code graph refresh |
| `/task-framework` | `/task-framework [description]` | Create + activate a task, explains the full task lifecycle |
| `/task-create` | `/task-create` | Jira-style issue creation — epic/story/task/bug/subtask hierarchy, templates, args |
| `/task-log-decision` | `/task-log-decision [text]` | Persist a design decision to the active task's checkpoint |
| `/pause` | `/pause` | Finish current action, save pending intent to task body, wait for user input |
| `/onboarding` | `/onboarding` | Interactive setup guide — walks a new teammate through full claude-hooks setup step by step |
| `/what-am-i-working-on` | `/what-am-i-working-on` | Cold-start orientation — recent prompts, tool calls, and task activations across sessions |
| `/deploy` | `/deploy` | Deploy claude-hooks dev→test→main — unit gate, full suite, then ship to main |
| `/task-introspection` | `/task-introspection [task:<id>]` | Post-task retrospective — surface unlogged decisions, stale memories, skill gaps, encode learnings |
| `/task-grooming` | `/task-grooming [task:<id> \| epic:<id>]` | Pre-work grooming — activate each task, audit body for gaps, update with findings before starting |

---

## Skill details

Full step-by-step instructions live in each skill file — these are what Claude reads at runtime. The notes below summarise when and why to use each skill.

### /gc

Stages all changes, runs unit tests, commits with a derived message, and appends `task:<id>` to the commit body automatically. Also refreshes code/diff embeddings after commit. Never pushes — push is a deliberate end-of-task action.

**Gate:** `GitCommitGate` blocks any commit missing a `task:<id>` in the body; `/gc` satisfies this automatically.

→ `skills/gc/skill.md`

---

### /deploy

Two-phase deploy: `deploy.sh` (dev → test, full suite, `launchctl kickstart -k` restart) then `deploy.sh --ship` (test → main). Always runs both the unit gate and the full integration suite before shipping.

If the health check fails after restart, stop and check `launchctl list | grep claude-hooks`.

→ `skills/deploy/skill.md`

---

### /task-framework

The entry point for tracked work. Creates a task, activates it for the session (which triggers related-task, diff-RAG, and memory injection), and defines the commit/close order. For multi-step work it proposes subtask decomposition and runs `/task-grooming` before any code is written.

→ `skills/task-framework/skill.md`

---

### /task-create

Quick reference for `tasks__create` — hierarchy rules (`epic → story/task/bug → subtask`), signatures, `cwd` vs `domain`, and the body format required by the gate. Use before any `tasks__create` call.

→ `skills/task-create/skill.md`

---

### /task-grooming

Pre-implementation review. Activates each task to pull injected context (related tasks, diff-RAG, memories), audits the body against six checks (resolution format, file paths, dependencies, conflicts, prior art, deferred decisions), appends gaps as a dated note, then resets to `open`. Run after creating subtasks, before writing any code.

→ `skills/task-grooming/skill.md`

---

### /task-introspection

Post-task retrospective. After a task closes, works through four questions — did it go as planned, unlogged decisions, stale memories, learnings to encode — and saves findings back to the task and memory system. Highest-value step is Q2 (unlogged decisions).

→ `skills/task-introspection/skill.md`

---

### /task-log-decision

Appends a design/architectural decision to the active task's checkpoint via `tasks__add_decision`. Decisions survive context compression and are injected every subsequent turn for that task.

→ `skills/task-log-decision/skill.md`

---

### /pause

Finishes any in-flight tool call, saves pending intent to the active task via `tasks__pause`, then waits for user input. Task stays active; history continues when the user resumes.

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
