---
tags: dev workflow, deploy.sh, committing, /commit, git commit, testing, deploy, RAG refresh, code embeddings, diff embeddings
---
# Development Workflow

## Overview

Single worktree, single branch:

```
~/workspace/claude-hooks/   ← main branch — all edits and the live hook server both live here (port 8766)
```

The dev/test/main three-worktree layout was retired — only main exists now.
Edits, commits, and the running server all live in the same checkout.

The hook server runs with **MemorySaver** — in-process, in-memory
checkpointing (task:b3964f85 retired `SqliteSaver` after two corruption
incidents). Checkpoint state does NOT survive server restarts;
`~/.claude/langgraph_checkpoints.db` is a retired file nothing writes to
anymore. `/deploy` restarts the server after each commit.

## Day-to-day loop

```bash
# 1. Edit

# 2. Quick unit tests (no server needed)
uv run python -m pytest tests/ -q -m "not integration"

# 3. Commit
/commit

# 4. Restart the server + run the full suite against it
/deploy
```

`/deploy` runs: unit tests → restart the server via launchctl → wait for
`/health` → run the full suite (unit + integration) against the live server.

## Key rules

| Rule | Why |
|------|-----|
| `/commit` commits target the main worktree | There is no separate dev branch anymore |
| `/deploy` is the way to restart the live server and confirm the full suite | Server code doesn't reload on its own — editing this repo doesn't change the running process until it restarts |
| Include `task:<id>` in every commit | `/commit` cites the active task via `tasks__format_commit_message` |

## Committing

Use `/commit` from any session.

```
feat(area): short description

task:abc123
epic:def456
```

## RAG index refresh after deploy

After every successful commit, refresh the code and diff indexes so search
stays in sync with HEAD (`/commit` does not run it; run it
manually):

```bash
# code_rag — incremental (changed files only)
mac code_rag index_files path/to/changed.py

# diff_rag — last commit
mac diff_rag index_commits --repo . --since HEAD~1 --max-commits 1
```

## Checkpoint state

The server uses `MemorySaver` (in-process, in-memory). State does NOT persist
across server restarts or redeploys — this is a deliberate tradeoff after
`SqliteSaver` corruption incidents (task:b3964f85). Design accordingly: don't
rely on checkpoint state surviving a `/deploy`.
