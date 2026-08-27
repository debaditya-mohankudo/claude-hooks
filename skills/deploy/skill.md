---
name: deploy
description: Deploy claude-hooks. Commit to main, run the unit gate, restart the hook server, then run the full suite (unit + integration) against the live server. Use when ready to ship a change to the running hooks.
user-invocable: true
updated: 2026-08-27
repo: ~/workspace/claude-hooks/skills/deploy/skill.md
deployed: ~/.claude/skills/deploy/skill.md
note: Canonical copy is the repo one. `~/.claude/skills/deploy/skill.md` is a hand-copied deploy target (no sync script) — keep the two identical.
---

Deploy `claude-hooks` to the running hook server.

**The dev→test→main three-worktree pipeline was retired.** Only `main` exists
now, at `~/workspace/claude-hooks`, and the hook server runs directly from it
via launchd (`com.debaditya.claude-hooks-pipeline`). There is no merge step and
no `--ship`. Deploying is: land the commit on `main`, then run `deploy.sh` to
prove the suite still passes and restart the server so it picks up the new code.

## Steps

### 1. Concept audit (before the commit)

Find the `.py` files this change touches:

```bash
git -C ~/workspace/claude-hooks diff --name-only HEAD | grep '\.py$'      # uncommitted
git -C ~/workspace/claude-hooks diff --name-only origin/main...HEAD | grep '\.py$'   # already committed, not pushed
```

For each changed `.py` file, look up stored concepts whose `module` matches.
**Use the `concept__list` MCP tool — never hand-parse `concept_store/concepts.json` directly.**

On disk the shape is `{"concepts": {"<slug>": {...}}, "meta": {...}}` — `concepts`
is a **name-keyed map**. The `concept__list` tool's *response* wraps results in a
list, which is why the snippet below iterates one; do not confuse the two.
Hand-parsing goes wrong by calling `.values()`/`.items()` on the **top level**,
which yields `"concepts"`/`"meta"` as if they were concepts — the fix is
`raw["concepts"]`, not a different shape. That bug let task:da29c842 ship without
its concept-store update caught here, found by chance afterward.

```python
concepts = mcp__claude-hooks__concept__list(repo="/Users/debaditya/workspace/claude-hooks")["concepts"]
changed = [...]  # from git diff above
hits = [c for c in concepts if c["module"] in changed]
```

For each hit, print:

```
concept: <name>  (<module>)
invariants:
  - <invariant 1>
contracts:
  - <contract 1>
```

Then ask the user:

> "This deploy touches N modules with stored concepts (listed above). Does the change respect, extend, or intentionally break any of these invariants/contracts?"

- **Respect** → proceed
- **Extend** → delegate the update to `/update-concept-store` rather than inlining a JSON-edit script:
  ```
  Skill(skill="update-concept-store", args="repo=~/workspace/claude-hooks touched_files=<changed files above> context=<what the change does and why, e.g. task:<id> resolution>")
  ```
  It updates the matched concept(s) in place and reports what changed. Fold the
  resulting `concepts.json` into the same commit as the code change (so the
  commit's own `tests/test_concepts.py` stays green — see Splitting below). Full
  reseed (`scripts/extract_concepts.py`) only if multiple modules changed
  substantially.
- **Intentionally break** → user must confirm explicitly; note the broken invariant in the commit message

Skip silently if `concepts.json` does not exist or no changed files match any concept.

### 2. Commit to main

`claude-hooks` commits go **directly to `main`** — there is no feature-branch
merge in this flow. If the working tree is dirty, commit it now:

- Every commit cites its task on the second line: a blank line, then `task:<id>`.
  Get the id from `tasks__active`; if there is none, ask which task this belongs
  to. Never invent one.
- Subject in present tense, em-dash, no trailing period — what became true.
- Body is for **why**, not a restatement of the diff.
- End the message with the co-author / session trailers this environment
  requires.
- Write the message to a scratchpad file and `git commit -F <path>` —
  heredocs and `-m` chains mangle multi-paragraph bodies.

After it lands, link it: `tasks__add_commit(task_id, sha=<git rev-parse HEAD>, repo="/Users/debaditya/workspace/claude-hooks")`.

If the commit touched a path named in a `models/*.sysml` package's
`modelledPaths`, re-stamp that package's `derivedFromCommit` in a **separate
follow-up commit** (grep every `models/*.sysml` for each touched path — one
change can stale more than one package). Re-read each stale package's prose
against the change before bumping.

### 3. Run deploy.sh

```bash
~/workspace/claude-hooks/scripts/deploy.sh
```

The script (`set -euo pipefail` — any failure aborts it):

1. **Unit gate** — `uv run python -m pytest tests/ -q -m "not integration"` from `~/workspace/claude-hooks`.
2. **Restart the server** — `launchctl kill SIGTERM gui/$(id -u)/com.debaditya.claude-hooks-pipeline`.
   Graceful SIGTERM, deliberately **not** `kickstart -k` (task:ac5df3db —
   `-k`'s faster kill path left `~/.claude/langgraph_checkpoints.db` throwing
   "attempt to write a readonly database" on every subsequent request until the
   next restart). `KeepAlive=true` in the plist respawns the process the moment
   it exits, so no `launchctl start` is needed.
3. **Health check** — polls `http://127.0.0.1:8766/health` up to 15×1s for
   `status: ok` (a fixed sleep has repeatedly been too short — checkpoint DB
   compaction at startup scales with DB size). Exits 1 if it never goes ok.
4. **Full suite** — unit (`-m "not integration"`) then integration
   (`-m "integration"`) as **two separate sequential runs**. Two reasons:
   `pyproject.toml`'s `addopts` bakes in `-m "not integration"`, which silently
   wins over a bare `pytest tests/` (every prior run before 2026-07-05 was
   skipping all integration tests while reporting success); and a combined
   `-m "integration or not integration"` run oversubscribes `-n auto` and flakes
   a timing-sensitive perf test. The script refuses a false-green if 0
   integration tests ran.

If any step fails, **stop and report which step and which tests**. The server
may or may not have restarted depending on where it aborted — say so.

### 4. Done

Report:
```
✓ Deployed to main.
  Unit gate:   passed
  Server:      restarted, health ok
  Full suite:  unit <N> passed, integration <M> passed
  main is at:  <git log --oneline -1 ~/workspace/claude-hooks>
```

## When the unit gate fails on something the change didn't introduce

`deploy.sh` has no skip. If it aborts at the unit gate on a failure your diff
didn't cause, confirm that (`git stash` + re-run the failing test, or run it on
`origin/main`), then **the fix is to get the suite green — not to work around
the script.** Either fix the failing test or, with the user's agreement,
quarantine it (`@pytest.mark.skip(reason=...)` with a tracking task). Until the
gate passes, the server has **not** restarted and the new code is not live.

If the user explicitly approves a one-time manual path, it is exactly the
script's own steps by hand: `launchctl kill SIGTERM gui/$(id -u)/com.debaditya.claude-hooks-pipeline`,
poll `/health` to ok, then `uv run python -m pytest tests/ -q -m "not integration"`
and `... -m "integration"` separately. Report every pre-existing failure that
was carried past.

## Splitting

Prefer one commit per idea. But `tests/test_concepts.py` binds each module to a
concept entry, so a split that separates a new module from its concept produces
a commit whose own tests fail. A green intermediate commit beats a tidy split —
commit the coupled work together and say in the body why.

## Rules

- Never skip the unit gate or full suite — don't pass `--no-verify`, don't comment out test steps, don't `-m` past the integration run.
- If the health check fails after restart, stop — the server didn't come back cleanly. Ask the user to check `launchctl list | grep claude-hooks` and the server log.
- If integration tests fail, stop and report which. The code is already on `main` at this point — a failing integration run means a fix-forward commit, not a silent revert.
- This skill only applies to `claude-hooks` at `~/workspace/claude-hooks`.
