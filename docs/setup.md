---
tags: project setup, installation, dependencies, uv, venv, iCloud Databases, hook registration, settings.json, FastAPI server, environment variables, prerequisites, first-time setup, clone, install
---
# Project Setup Guide

How to get claude-hooks running from scratch on a new machine or when sharing the project with someone.

---

## Prerequisites

- macOS with [Claude Code](https://claude.ai/code) installed
- [uv](https://docs.astral.sh/uv/) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- [Ollama](https://ollama.com/download) installed and the `nomic-embed-text` model pulled:

  ```bash
  brew install ollama
  ollama pull nomic-embed-text
  ```

  Ollama powers semantic search (task neighbors, code RAG, vault RAG). Without it the MCP server starts but context injection and `tasks__neighbors` will not work.
- iCloud Drive enabled (two databases live there — see below)

---

## 1. Clone and install dependencies

```bash
git clone https://github.com/debaditya-mohankudo/Lite-Task-Framework-w-Claude-hooks ~/workspace/claude-hooks
cd ~/workspace/claude-hooks
uv sync
```

---

## 2. Create the iCloud Databases directory

Two databases live in iCloud so they sync across machines:

```bash
mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/Databases
```

If iCloud is not available, set the override env var (see [Environment variables](#6-environment-variables)):

```bash
export CLAUDE_HOOKS_ICLOUD_DB_DIR=~/.claude/databases
mkdir -p ~/.claude/databases
```

---

## 3. Create the databases

All databases are auto-created on first use — nothing to create manually.

| Database | Location | Created by |
| --- | --- | --- |
| `MEMORY.sqlite` | `~/.claude/MEMORY.sqlite` | First `memory__add` MCP call |
| `proj_tasks.db` | `~/.claude/proj_tasks.db` | First `tasks__create` MCP call |
| `tool_hints.sqlite` | iCloud `Databases/tool_hints.sqlite` | First `post_tool_use` hook run |
| `claude_hooks.sqlite` | iCloud `Databases/claude_hooks.sqlite` | First hook run |
| `.tasks_embeddings.tvim` | repo root | MCP server startup (auto-rebuilt if missing) |

### Configuring your projects

CWD→domain mappings are declared in `CWD_DOMAIN_MAP` in `src/config.py` — no external file needed. Each key is a CWD substring matched case-insensitively; first match wins. Add an entry for each repo you want claude-hooks to recognise:

```python
# src/config.py
CWD_DOMAIN_MAP: dict[str, str] = {
    "claude-hooks": "claude-hooks",
    "vault":        "vault",
    "market-intel": "market-intel",
    "<repo-dirname>": "<domain-name>",  # ← add your repo here
}
```

If the domain is new, also add it to `VALID_DOMAINS` in the same file. The change takes effect on the next hook run — no restart needed.

Without an entry, prompts from that repo fall back to `global` domain and memories won't be project-scoped.

**Worked example (claude-hooks itself):**

| Config | Value |
| --- | --- |
| `CWD_DOMAIN_MAP` key | `claude-hooks` |
| domain | `claude-hooks` |
| Goals memory | `claude-hooks-goals` (priority 1) |
| Arch memory | `claude-hooks-arch` (priority 10) |

---

## 4. Start the FastAPI hook server

The hook server is a persistent FastAPI process that handles all four hook events. It must be running before Claude Code fires any hooks.

```bash
cd ~/workspace/claude-hooks
scripts/install_server.sh   # installs the launchd plist and starts the server
```

This registers `hooks.server` as a `launchd` user agent that starts automatically on login. The server listens on `http://127.0.0.1:8766`.

**Manual start (without launchd):**

```bash
uv run uvicorn hooks.server:app --host 127.0.0.1 --port 8766
```

> Do not use `--reload` — it restarts the worker on every file save, wiping the
> in-process MemorySaver checkpoint (active task + session context). The deploy
> workflow handles restarts explicitly via `pkill` + relaunch.

**Verify the server is up:**

```bash
curl http://127.0.0.1:8766/health
# → {"status":"ok","sessions":0}
```

---

## 5. Optional: Copilot-compatible bridge

Copilot does not expose the same native hook event surface as Claude Code, so this repo now includes a small bridge script that forwards simple prompt/tool events to the same FastAPI hook server.

```bash
cd ~/workspace/claude-hooks
python3 hooks/copilot_client.py prompt --prompt "summarize this repo" --session-id copilot-demo
python3 hooks/copilot_client.py pre-tool --tool-name imessage__send --session-id copilot-demo --tool-args '{"recipient":"+1-555-0100"}'
python3 hooks/copilot_client.py post-tool --tool-name imessage__send --session-id copilot-demo --tool-args '{"recipient":"+1-555-0100"}'
```

The bridge maps these to the equivalent `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop` endpoint calls so the existing gates and context pipeline can be exercised from Copilot-style workflows.

---

## 6. Register the hooks in `~/.claude/settings.json`

Hooks now call `client.py` — a thin HTTP wrapper (stdlib urllib, no curl/jq needed) that posts to the FastAPI server. Add the following to your global Claude Code settings (`~/.claude/settings.json`). Replace the path if you cloned to a different location.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/<you>/workspace/claude-hooks/hooks/client.py UserPromptSubmit"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/<you>/workspace/claude-hooks/hooks/client.py PreToolUse"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/<you>/workspace/claude-hooks/hooks/client.py PostToolUse"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/<you>/workspace/claude-hooks/hooks/client.py Stop"
          }
        ]
      }
    ]
  }
}
```

`client.py` enriches the payload with `CLAUDE_CWD` and POSTs it to the server. If the server is unreachable, it falls back to `dispatcher.py` (subprocess mode) so hooks never silently disappear.

---

## 7. Environment variables

All variables are optional. Set them in `~/.claude/.env` or export in your shell.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_HOOKS_ICLOUD_DB_DIR` | `~/Library/Mobile Documents/.../Databases` | Override iCloud path (e.g. when iCloud unavailable) |
| `CLAUDE_HOOKS_MEMORY_DB` | `~/.claude/MEMORY.sqlite` | Override memory DB path |
| `LC_DEV_MODE` | `false` | Set `true` to surface hook errors inline in Claude Code (exit 2 on exception). Use during development only. |
| `LC_TOP_K` | `7` | Max number of scored memories returned per prompt |

---

## 8. Verify the setup

Start a new Claude Code session and check the system prompt for `## Injected memories`. If the block appears, the `UserPromptSubmit` hook is running correctly.

The MCP server rebuilds the task semantic index (`.tasks_embeddings.tvim`) on startup if it is missing. This requires Ollama with `nomic-embed-text` running locally. If Ollama is unavailable at startup the server still starts — the index is built lazily on the first `tasks__neighbors` call instead.

To check hook logs:

```text
mcp__claude-hooks__hooks__read_logs_sqlite
```

To test manually:

```bash
cd ~/workspace/claude-hooks
echo '{"session_id":"test","prompt":"test prompt","cwd":"/tmp"}' | \
  uv run python3 ~/.claude/hooks/dispatcher.py UserPromptSubmit
```

A successful run exits 0 and emits JSON with `additionalSystemPrompt`.

---

## 9. Seed initial memories (optional but recommended)

The system works without any memories, but seeding a few facts immediately improves context quality. **Since task:850ddd65's memory split, which store you seed into depends on what kind of fact it is** — this replaces an earlier version of this section that recommended seeding `type=project` facts via `memory__add` with a nonexistent `priority` param; that per-memory priority field never existed in the schema (`src/db/schema.py`'s `memories` table has no such column), and project-level architecture facts belong in the per-repo store below, not the always-injected global one.

### Repo-specific architecture facts → `repo_memory__upsert` (lifecycle-scoped, not per-turn injected)

Facts about *this repo's* code/architecture — stack, key files, invariants — go to the committed per-repo store, mirroring `concept_store`'s pattern. This is consulted during task-grooming/task-introspection/task-activation, **not** injected into every UPS turn:

```python
mcp__claude-hooks__repo_memory__upsert(
    repo="<absolute path to repo>",
    memory={
        "name": "<repo>-arch",
        "type": "project",  # or "reference"
        "body": """Stack: <language, frameworks>

Key files:
- <file1> — <purpose>
- <file2> — <purpose>

Databases / external deps:
- <db or service> — <purpose>""",
        "tags": "<domain>,architecture,files,stack",
        "files": "<file1>, <file2>",
    },
)
```

### Cross-cutting mission/goals note → `memory__add` (global, per-turn injected)

A short "what this project is trying to do and what distracts from it" note is genuinely useful on every turn, so it stays in the global store (`type="project"`, tagged with the repo's `domain` from `cwd_domains.json`):

```python
mcp__claude-hooks__memory__add(
    name="<repo>-goals",
    type="project",
    domain="<domain>",
    tags="<domain>,goals,mission,direction",
    body="""Most important goal: <one-line mission statement>

<project description, current direction, key constraints>

What recency pull looks like for this project — recognise and resist:
- <distraction pattern 1>
- <distraction pattern 2>

The test: at the end of a session, did the work move the mission forward?"""
)
```

See [ARCHITECTURE.md](ARCHITECTURE.md)'s subsystem section for why this split exists and which consumer (task activation, task-grooming, task-introspection) reads `repo_memory` vs. the always-on UPS retrieval pipeline.

Add further memories (feedback, reference) as the project evolves.

---

## Troubleshooting

**Hooks not firing** — check that `~/.claude/settings.json` has the correct absolute path to the repo and that `uv` is on PATH (`which uv`). Use the full path `/Users/<you>/.local/bin/uv` if needed.

**iCloud path errors** — set `CLAUDE_HOOKS_ICLOUD_DB_DIR` to a local directory and restart.

**Silent failures** — set `LC_DEV_MODE=true` in `~/.claude/.env` to make hook errors surface inline in Claude Code.

---

← [Architecture](ARCHITECTURE.md)
