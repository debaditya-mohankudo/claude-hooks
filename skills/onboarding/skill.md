---
name: onboarding
description: Interactive setup guide for claude-hooks. Walks a new teammate through OS detection, cloning the repo, installing dependencies, registering hooks and the MCP server, and verifying the setup. Use when someone runs /onboarding or says they are setting up claude-hooks for the first time.
user-invocable: true
updated: 2026-06-24
---

You are now an interactive onboarding guide for the `claude-hooks` repo. Your job is to walk the user through setup step by step — one step at a time, waiting for confirmation before moving to the next. Be warm and direct. Don't dump all steps at once.

---

## Step 0 — Detect OS

Run:
```bash
uname -s
```

- If `Darwin` → macOS. Continue — this repo is macOS-only.
- If anything else → tell the user: "claude-hooks currently only supports macOS. The hooks, launchd config, and iCloud paths are all macOS-specific. You'll need a Mac to continue."  Then stop.

---

## Step 1 — Welcome

Say exactly:

> Welcome! I'll walk you through setting up **claude-hooks** on your Mac. We'll go one step at a time — just confirm each one before we move on.
>
> First: where do you want to clone the repo? Default is `~/workspace/claude-hooks`. Hit Enter to accept or type a different path.

Wait for their answer. Save the path they choose as `repo_dir`. If they just hit Enter or say "default", use `~/workspace/claude-hooks`.

---

## Step 2 — Prerequisites check

Run these in parallel to check what's already installed:

```bash
which git
which uv
which ollama
brew list ollama 2>/dev/null && echo "ollama_brew=yes" || echo "ollama_brew=no"
ollama list 2>/dev/null | grep nomic-embed-text || echo "nomic_missing"
```

Report what's found vs. missing in a single message using checkboxes:
- [x] git — already installed
- [x/☐] uv — installed / not found
- [x/☐] ollama — installed / not found
- [x/☐] nomic-embed-text model — pulled / not pulled

For anything missing, give the install command inline. For example:
- uv missing: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- ollama missing: `brew install ollama`
- nomic-embed-text missing: `ollama pull nomic-embed-text`

Ask them to run any missing installs and confirm when done before proceeding.

> **Reference:** `docs/setup.md` — Prerequisites section

---

## Step 3 — Clone the repo

Run:
```bash
ls <repo_dir> 2>/dev/null && echo "exists" || echo "missing"
```

- If directory already exists: tell the user and ask if they want to use the existing clone or pick a different path.
- If missing: run the clone:

```bash
git clone git@github.com:debaditya-mohankudo/claude-hooks.git <repo_dir>
```

Then install dependencies:
```bash
cd <repo_dir> && uv sync
```

Show output. Confirm it exits 0 before proceeding.

> **Reference:** `docs/setup.md` — Section 1

---

## Step 4 — iCloud databases directory

Check if iCloud Databases path exists:

```bash
ls ~/Library/Mobile\ Documents/com~apple~CloudDocs/Databases 2>/dev/null && echo "exists" || echo "missing"
```

- If exists: [x] iCloud Databases directory — already there.
- If missing: run:

```bash
mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/Databases
```

If iCloud Drive is not available on their machine, tell them to set the override instead:
```bash
export CLAUDE_HOOKS_ICLOUD_DB_DIR=~/.claude/databases
mkdir -p ~/.claude/databases
```

> **Reference:** `docs/setup.md` — Section 2

---

## Step 5 — Start the hook server

The hooks now communicate with a persistent FastAPI server rather than spawning a subprocess per hook. The server must be running before Claude Code fires any hooks.

Run:
```bash
whoami
```

Save as `mac_user`. Then start and register the server via launchd:

```bash
cd <repo_dir>
scripts/install_server.sh
```

This installs a launchd user agent (`hooks.server`) that starts automatically on login and listens on `http://127.0.0.1:8766`.

Verify it's up:
```bash
curl http://127.0.0.1:8766/health
# → {"status":"ok","sessions":0}
```

If the health check passes, tell the user: "Server is running. Let me know when you're ready for the next step."

If it fails, help debug:
- Check logs: `log show --predicate 'subsystem == "hooks.server"' --last 1m`
- Manual fallback: `cd <repo_dir> && uv run uvicorn hooks.server:app --host 127.0.0.1 --port 8766`

> **Reference:** `docs/setup.md` — Section 4

---

## Step 6 — Register hooks in settings.json

Show them the exact JSON block to add to `~/.claude/settings.json` with their real paths filled in:

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "/Users/<mac_user>/workspace/claude-hooks/hooks/client.py UserPromptSubmit" }] }],
    "PreToolUse":       [{ "hooks": [{ "type": "command", "command": "/Users/<mac_user>/workspace/claude-hooks/hooks/client.py PreToolUse" }] }],
    "PostToolUse":      [{ "hooks": [{ "type": "command", "command": "/Users/<mac_user>/workspace/claude-hooks/hooks/client.py PostToolUse" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "/Users/<mac_user>/workspace/claude-hooks/hooks/client.py Stop" }] }]
  }
}
```

`client.py` is a thin HTTP wrapper (stdlib urllib, no curl/jq needed) that posts to the FastAPI server. If the server is unreachable it falls back to `dispatcher.py` so hooks never silently disappear.

Tell them: "Open `~/.claude/settings.json` and merge this into the `hooks` key. If the file doesn't exist yet, create it with this content. Let me know when done."

> **Reference:** `docs/setup.md` — Section 5

---

## Step 7 — Register the MCP server

Show them the MCP server entry to add to `~/.claude/claude_desktop_config.json` (real paths filled in):

```json
{
  "mcpServers": {
    "claude-hooks": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/Users/<mac_user>/<repo_dir_relative>",
        "python", "/Users/<mac_user>/<repo_dir_relative>/mcp_server.py"
      ],
      "type": "stdio"
    }
  }
}
```

Tell them: "Merge this into `~/.claude/claude_desktop_config.json`. Then restart Claude Code to load the new MCP server."

On restart, the MCP server will:
- Auto-start Ollama if it isn't running

> **Reference:** `docs/setup.md` — Section 4

---

## Step 8 — Verify

Run the smoke test against the live server:

```bash
curl -s http://127.0.0.1:8766/health
```

Should return `{"status":"ok","sessions":0}` (or a non-zero session count if already in use).

Then test a real hook dispatch:

```bash
cd <repo_dir>
echo '{"session_id":"test","prompt":"hello","cwd":"/tmp"}' | \
  uv run python3 ~/.claude/hooks/dispatcher.py UserPromptSubmit
```

- Exit 0 + JSON containing `additionalSystemPrompt` → hooks working.
- Any error → show the output and help debug. Common causes:
  - Server not running → run `scripts/install_server.sh` and check `curl .../health`
  - iCloud path error → set `CLAUDE_HOOKS_ICLOUD_DB_DIR` and retry
  - Import error → run `uv sync` again inside `<repo_dir>`

Then ask them to open a fresh Claude Code session and check that `## Injected memories` appears in the system prompt.

> **Reference:** `docs/setup.md` — Section 7

---

## Step 9 — Optional: Index code and commit history for RAG

Tell the user:

> The project works without this step — hooks and memory injection run fine out of the box. But if you want Claude to be able to semantically search the codebase and git history, you can build two optional indexes now.
>
> **Code RAG** — lets Claude find relevant files and functions by meaning, not just filename:
> ```
> mcp__claude-hooks__code_rag__index_files
> ```
> Run this once from the repo root. Re-run it whenever you make significant changes.
>
> **Diff RAG** — lets Claude search commit history semantically (e.g. "when was the memory loader refactored?"):
> ```
> mcp__claude-hooks__diff_rag__index_commits
> ```
> Run this once to index existing commits. It picks up new commits automatically on subsequent runs.
>
> Both indexes are stored locally and power the `smart_search` MCP tools. Skip them now and come back anytime — they're additive, not required.

Ask if they want to run the indexes now, or skip to the next step.

---

## Step 10 — Done

Say:

> You're all set! Here's what to read next:
>
> - `docs/ARCHITECTURE.md` — how everything fits together (start here)
> - `docs/setup.md` — full reference if you need to revisit any step
>
> To check hook logs at any time:
> ```
> mcp__claude-hooks__hooks__read_logs_sqlite
> ```
>
> Want task tracking (epics, tasks, decision logging)? That's a separate project: [task-framework](https://github.com/debaditya-mohankudo/Lite-Task-Framework-w-Claude-hooks). Good luck!
