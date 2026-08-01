# Contributing

## How the repo is structured

claude-hooks uses three git worktrees so the live hook server is never disrupted by in-progress edits:

| Worktree | Branch | Purpose |
|---|---|---|
| `~/workspace/claude-hooks-dev` | `dev` | All edits happen here |
| `~/workspace/claude-hooks-test` | `test` | Hook server runs here (port 8766) |
| `~/workspace/claude-hooks` | `main` | Production — only receives clean merges via `/deploy` |

**Never edit `test` or `main` directly.**

`main` has GitHub branch protection requiring a pull request before merging (no
required reviews — it just blocks direct pushes). Admins are exempted, so
`deploy.sh`'s local merge-and-push from `~/workspace/claude-hooks` still works
unchanged; the protection exists to stop anyone/anything else from pushing to
`main` outside the dev→test→main path.

## Making a change

```bash
# 1. Edit in dev
cd ~/workspace/claude-hooks-dev

# 2. Run unit tests (fast, no server needed)
uv run python -m pytest tests/ -q -m "not integration"

# 3. Commit
/gc

# 4. Deploy to test, run full suite, ship to main
/deploy
```

`/deploy` handles everything: merges dev→test, restarts the server, runs the full test suite (unit + integration), then merges test→main. Don't run `deploy.sh` directly.

## Running tests

```bash
# Unit tests only (fast, run locally any time)
uv run python -m pytest tests/ -q -m "not integration"

# Full suite — run via /deploy (requires the hook server on port 8766)
uv run python -m pytest tests/ -q
```

## Hook server

The server runs from `~/workspace/claude-hooks-test`. `/deploy` restarts it automatically after merging dev→test so new code is live before the test suite runs.

Check server health:
```bash
curl http://127.0.0.1:8766/health
```

Check logs:
```bash
# Via MCP tool (preferred)
mcp__claude-hooks__hooks__read_logs_sqlite

# Or tail the raw log
tail -f /tmp/claude-hooks-pipeline.log
```

## Prerequisites

See [docs/setup.md](docs/setup.md) for the full install guide.
