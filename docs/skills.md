---
tags: skills, /add-memory, /commit, /deploy, /pause, /onboarding, /what-am-i-working-on, skill index, slash commands, workflow skills
---
# Claude-hooks Skills

Skills live in `skills/<name>` and are synced to `~/.claude/skills/<name>` after every change. Invoke with `/<name>` in any Claude session.

> Task-lifecycle skills (`/task-framework`, `/task-create`, `/task-grooming`, `/task-implementation`, `/task-introspection`, `/task-log-decision`) have moved to the separate [task-framework](https://github.com/debaditya-mohankudo/Lite-Task-Framework) project. `/commit` is a global skill in `~/.claude/skills/commit`, not shipped from this repo.

## Skills index

| Skill | Invoke | Purpose |
| --- | --- | --- |
| `/pause` | `/pause` | Finish current action, save pending intent (to the active task, if one exists), wait for user input |
| `/deploy` | `/deploy` | Run the unit gate, restart the hook server via launchd, run the full suite against it |
| `/onboarding` | `/onboarding` | Interactive setup guide — walks a new teammate through full claude-hooks setup step by step |
| `/add-memory` | `/add-memory` | Save to MEMORY.sqlite the right way — search first, pick type/slug/tags/links, write via `mac memory add`, verify |
| `/what-am-i-working-on` | `/what-am-i-working-on` | Cold-start orientation — recent prompts, tool calls, and task activations across sessions |

---

## Skill details

Full step-by-step instructions live in each skill file — these are what Claude reads at runtime. The notes below summarise when and why to use each skill.

### /pause

Finishes any in-flight tool call, saves pending intent (to the active task via `mcp__taskfw__tasks__update`, if [task-framework](https://github.com/debaditya-mohankudo/Lite-Task-Framework) is installed and a task is active), then waits for user input. Task stays active; history continues when the user resumes.

→ `skills/pause/skill.md`

---

### /what-am-i-working-on

Calls `hooks__server_memory(n_events=50)` and presents the returned timeline — recent prompts, tool calls, and task activations across sessions. Use at the start of a fresh session for quick orientation.

→ `skills/what-am-i-working-on/skill.md`

---

### /add-memory

Think-first writer for `MEMORY.sqlite`: is it worth saving, new vs update, slug and BM25 tags, typed links, then `mac memory add` and a read-back. Guards the re-add traps (omitted `--related` wipes links; placeholder body overwrites the real one). Deployed copy is `~/.claude/skills/add-memory/SKILL.md`.

→ `skills/add-memory/skill.md`

---

### /deploy

Runs the concept audit, commits to `main`, then `scripts/deploy.sh`: unit gate → launchctl restart → `/health` → unit then integration suites against the live server. `scripts/deploy.sh --restart-only` skips both test steps and only restarts and health-checks.

→ `skills/deploy/skill.md` · `scripts/deploy.sh`

---

### /onboarding

Interactive setup guide for a new teammate. Steps: OS detection → prerequisites → clone/deps → hook registration → MCP server → smoke test. Goes one step at a time waiting for confirmation.

→ `skills/onboarding/skill.md` · reference: `docs/setup.md`

---

## Syncing skills to ~/.claude

After editing any skill file in `skills/`, sync it:

```bash
cp skills/<name>/skill.md ~/.claude/skills/<name>/skill.md
```

The repo is the source of truth — `~/.claude/skills/` is the deployed copy.

---

## Skill-tagged memories

Tag a memory `skill-<name>` (e.g. `skill-stock-tracker`) and it is injected, by exact tag match, when the
Skill tool runs `<name>` (max 3, newest first, superseded ones dropped). A skill that saves memories should say
in its SKILL.md to pass that tag in `--tags`. Saving a memory after a skill ran without a `skill-*` tag draws a
reminder; re-adding a slug keeps its stored `skill-*` tags. Plugin prefixes are dropped (`plugin:pdf` -> `skill-pdf`).

---

← [Architecture](ARCHITECTURE.md) · [Setup](setup.md)
