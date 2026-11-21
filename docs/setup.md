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

  Ollama powers semantic search (code RAG, diff RAG). Without it the MCP server starts but the `code_rag__*` / `diff_rag__*` tools will not work; memory injection (BM25) does not need it.
- iCloud Drive enabled (the LIFE_OS vault and two JSON config files live there — see below)

---

## 1. Clone and install dependencies

```bash
git clone https://github.com/debaditya-mohankudo/claude-hooks ~/workspace/claude-hooks
cd ~/workspace/claude-hooks
uv sync
```

---

## 2. Create the iCloud Databases directory

The SQLite databases all live under `~/.claude` (created automatically). iCloud
still holds two JSON config files (`memory_scoring.json`, `tool_registry.json`)
so scoring tunables and tool mappings sync across machines:

```bash
mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/Databases
```

If iCloud is not available, set the override env var (see [Environment variables](#6-environment-variables)):

```bash
export CLAUDE_HOOKS_ICLOUD_DB_DIR=~/.claude/databases
mkdir -p ~/.claude/databases
```

> `tool_hints.sqlite` and `claude_hooks.sqlite` used to live in iCloud too;
> task:62ea0ea5 moved them to `~/.claude` — an iCloud-synced SQLite file on
> the hook write path caused transient read locks / `EDEADLK`. Override their
> directory with `CLAUDE_HOOKS_CLAUDE_DB_DIR` if `~/.claude` is not where you
> want them.

---

## 3. Create the databases

All databases are auto-created on first use — nothing to create manually.

| Database | Location | Created by |
| --- | --- | --- |
| `MEMORY.sqlite` | `~/.claude/MEMORY.sqlite` | First `memory__add` MCP call |
| `tool_hints.sqlite` | `~/.claude/tool_hints.sqlite` | First `post_tool_use` hook run |
| `claude_hooks.sqlite` | `~/.claude/claude_hooks.sqlite` | First hook run |
| `server_memory.sqlite` | `~/.claude/server_memory.sqlite` | First hook run (rolling cross-session event log) |
| `.code_embeddings.tvim`, `.diff_embeddings.tvim` | repo root of each indexed repo | `mac code_rag index_files` / `mac diff_rag index_commits` |

### Configuring your projects

There is nothing to register per repo. CWD→domain mapping (`CWD_DOMAIN_MAP`, `cwd_domains.json`) and the `cwd_domain_detect` node were removed, and memories no longer carry a domain column — scoping is by `tags` and slug words (BM25). To make a memory surface for a project, give it tags that match how you talk about that project (see §8).

---

## 4. Start the FastAPI hook server

The hook server is a persistent FastAPI process that handles the hook events (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, and `SessionStart`/`SessionEnd`). It must be running before Claude Code fires any hooks.

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
> in-process MemorySaver checkpoint (session context). `scripts/deploy.sh` restarts
> the server explicitly via launchctl.

**Verify the server is up:**

```bash
curl http://127.0.0.1:8766/health
# → {"status":"ok"}
```

---

## 5. Register the hooks in `~/.claude/settings.json`

Hooks now call `client.py` — a thin HTTP wrapper (stdlib urllib, no curl/jq needed) that posts to the FastAPI server. Add the following to your global Claude Code settings (the server also handles `SessionStart` and `SessionEnd`; register them the same way if you want them) (`~/.claude/settings.json`). Replace the path if you cloned to a different location.

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

## 6. Environment variables

All variables are optional. Set them in `~/.claude/.env` or export in your shell.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_HOOKS_CLAUDE_DB_DIR` | `~/.claude` | Override the dir holding `tool_hints.sqlite` + `claude_hooks.sqlite` |
| `CLAUDE_HOOKS_ICLOUD_DB_DIR` | `~/Library/Mobile Documents/.../Databases` | Override iCloud path — now only the JSON config files (`memory_scoring.json`, `tool_registry.json`) |
| `CLAUDE_HOOKS_MEMORY_DB` | `~/.claude/MEMORY.sqlite` | Override memory DB path |
| `LC_DEV_MODE` | `false` | Set `true` to surface hook errors inline in Claude Code (exit 2 on exception). Use during development only. |
| `LC_TOP_K` | `7` | Max number of scored memories returned per prompt |

---

## 7. Verify the setup

Start a new Claude Code session and check the system prompt for `## Injected memories`. If the block appears, the `UserPromptSubmit` hook is running correctly.

To check hook logs:

```text
mac hooks read_logs_sqlite
```

To test manually:

```bash
cd ~/workspace/claude-hooks
echo '{"session_id":"test","prompt":"test prompt","cwd":"/tmp"}' | \
  uv run python3 hooks/dispatcher.py UserPromptSubmit
```

A successful run exits 0 and emits JSON with `additionalContext`.

---

## 8. Seed initial memories (optional but recommended)

The system works without any memories, but seeding a few facts immediately improves context quality. All memories — project architecture facts, cross-cutting mission notes, feedback, reference — live in the single global `MEMORY.sqlite` store, scored per-turn by BM25 over each memory's tags and slug words:

```bash
mac memory add <repo>-arch --type project \
    --tags "<repo>,architecture,files,stack" \
    --files "<file1>, <file2>" --body-file arch.md
mac memory add <repo>-goals --type project \
    --tags "<repo>,goals,mission,direction" --body-file goals.md
```

`arch.md`:

```text
Stack: <language, frameworks>

Key files:
- <file1> — <purpose>
- <file2> — <purpose>

Databases / external deps:
- <db or service> — <purpose>
```

`goals.md`:

```text
Most important goal: <one-line mission statement>

<project description, current direction, key constraints>

What recency pull looks like for this project — recognise and resist:
- <distraction pattern 1>
- <distraction pattern 2>

The test: at the end of a session, did the work move the mission forward?
```

Add further memories (feedback, reference) as the project evolves.

---

## Troubleshooting

**Hooks not firing** — check that `~/.claude/settings.json` has the correct absolute path to the repo and that `uv` is on PATH (`which uv`). Use the full path `/Users/<you>/.local/bin/uv` if needed.

**iCloud path errors** — the SQLite DBs are under `~/.claude` and don't need iCloud; for the JSON config files set `CLAUDE_HOOKS_ICLOUD_DB_DIR` to a local directory and restart. Override the SQLite DB dir with `CLAUDE_HOOKS_CLAUDE_DB_DIR`.

**Silent failures** — set `LC_DEV_MODE=true` in `~/.claude/.env` to make hook errors surface inline in Claude Code.

---

← [Architecture](ARCHITECTURE.md)
