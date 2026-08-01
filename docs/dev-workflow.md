---
tags: dev workflow, git worktree, deploy.sh, committing, /gc, git commit, testing, deploy, main worktree, test worktree, dev worktree, claude-hooks-dev, claude-hooks-test, branch, merge, RAG refresh, code embeddings, diff embeddings
---
# Development Workflow — Git Worktree

## Overview

Three separate worktrees, each on its own branch:

```
~/workspace/claude-hooks-dev/   ← dev branch   — all edits happen here
~/workspace/claude-hooks-test/  ← test branch  — the live hook server runs from here (port 8766)
~/workspace/claude-hooks/       ← main branch  — production, touched only by /deploy --ship
```

The hook server runs from **`claude-hooks-test`** with **MemorySaver** — in-process,
in-memory checkpointing (task:b3964f85 retired `SqliteSaver` after two corruption
incidents). Checkpoint state does NOT survive server restarts;
`~/.claude/langgraph_checkpoints.db` is a retired file nothing writes to anymore.
`/deploy` restarts the server after each merge into test.

Editing in `claude-hooks-dev` never disrupts the live server — it only runs
code from `claude-hooks-test`.

## Day-to-day loop

```bash
# 0. Merge main into dev first (task:701215e2) — catches any commits main has
#    that dev doesn't (e.g. an out-of-band direct-to-main edit) before they can
#    cause a real conflict at /deploy --ship time instead of here, where it's
#    cheap and obvious to resolve.
cd ~/workspace/claude-hooks-dev
git merge main --no-edit

# 1. Edit in the dev worktree

# 2. Quick unit tests (no server needed)
uv run python -m pytest tests/ -q -m "not integration"

# 3. Commit
/gc

# 4. Deploy to test + full suite + ship to main
/deploy
```

`/deploy` runs: unit tests in dev (quick gate) → merge dev→test → restart the
test-worktree server via launchctl → wait for `/health` → run the full suite
(unit + integration) from test against the live server. `/deploy --ship` then
merges test→main; no tests run at that step since they already passed in test.

## Key rules

| Rule | Why |
|------|-----|
| Edits go in `~/workspace/claude-hooks-dev` (dev branch) | Never touch main or test directly |
| **Merge main into dev before committing new work**, not just before `/deploy` | main can drift ahead via direct edits (has happened more than once) — catching it early avoids a conflict during `/deploy --ship` |
| `/gc` commits target `--repo ~/workspace/claude-hooks-dev` | Commits land on dev branch |
| Server runs from `claude-hooks-test`, restarted by `/deploy` | Dev edits never disrupt live Claude Code hooks |
| `/deploy` / `/deploy --ship` is the only path to merge dev→test→main | Keeps main always passing tests |
| Include `task:<id>` in every commit | `/gc` injects it automatically when a task is active |

## Committing

Use `/gc` from any session. The skill targets the dev worktree automatically.

```
feat(area): short description

task:abc123
epic:def456
```

## RAG index refresh after deploy

After every successful commit, refresh the code and diff indexes so search
stays in sync with HEAD (handled automatically by `/gc`, but can be run
manually):

```python
# code_rag — incremental (changed files only)
mcp__claude-hooks__code_rag__index_files(files=["path/to/changed.py"])

# diff_rag — last commit
mcp__claude-hooks__diff_rag__index_commits(repo=".", since="HEAD~1", max_commits=1)
```

## Checkpoint state

The server uses `MemorySaver` (in-process, in-memory). State does NOT persist
across server restarts or redeploys — this is a deliberate tradeoff after
`SqliteSaver` corruption incidents (task:b3964f85). Design accordingly: don't
rely on checkpoint state surviving a `/deploy`.
