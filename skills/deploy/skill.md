---
name: deploy
description: Deploy claude-hooks dev→test→main. Runs unit gate in dev, merges to test, runs full suite (unit + integration) from test, then ships to main. Use when ready to ship a feature branch to production.
user-invocable: true
updated: 2026-07-27
repo: ~/workspace/claude-hooks/skills/deploy/skill.md
deployed: ~/.claude/skills/deploy/skill.md
---

Deploy `claude-hooks` through the full pipeline: dev → test → main.

## Steps

### 1. Concept audit (before merge)

Read the diff between dev and test to find changed files:

```bash
git -C ~/workspace/claude-hooks-dev diff origin/test --name-only | grep '\.py$'
```

For each changed `.py` file, look up stored concepts whose `module` matches. **Use the `concept__list` MCP tool — never hand-parse `concept_store/concepts.json` directly.**

On disk the shape is `{"concepts": {"<slug>": {...}}, "meta": {...}}` — `concepts` is a **name-keyed map**. The `concept__list` tool's *response* wraps results in a list, which is why the snippet below iterates one; do not confuse the two. Hand-parsing goes wrong by calling `.values()`/`.items()` on the **top level**, which yields `"concepts"`/`"meta"` as if they were concepts — the fix is `raw["concepts"]`, not a different shape. That bug let task:da29c842 ship without its concept-store update caught here, found by chance afterward. *(Shape claim corrected 2026-08-01; the code below was always right.)*

```python
concepts = mcp__claude-hooks__concept__list(repo="/Users/debaditya/workspace/claude-hooks-dev")["concepts"]
changed = [...]  # from git diff above
hits = [c for c in concepts if c["module"] in changed]
```

For each hit, print:

```
concept: <name>  (<module>)
invariants:
  - <invariant 1>
  - <invariant 2>
contracts:
  - <contract 1>
```

Then ask the user:

> "This deploy touches N modules with stored concepts (listed above). Does the change respect, extend, or intentionally break any of these invariants/contracts?"

- **Respect** → proceed
- **Extend** → delegate the actual update to `/update-concept-store` rather than inlining a JSON-edit script here:
  ```
  Skill(skill="update-concept-store", args="repo=~/workspace/claude-hooks-dev touched_files=<changed files above> context=<what the deploy changes and why, e.g. task:<id> resolution>")
  ```
  It updates the matched concept(s) in place and reports what changed — then commit the resulting `concepts.json` to dev with the same task:<id>. Full reseed (`scripts/extract_concepts.py`) only if multiple modules changed substantially — `/update-concept-store` is for reconciling known changes, not bulk re-extraction.
- **Intentionally break** → user must confirm explicitly; note the broken invariant in the commit message

Skip silently if `concepts.json` does not exist or no changed files match any concept.

### 2. Deploy dev → test and run full suite

```bash
~/workspace/claude-hooks/scripts/deploy.sh
```

This script:
- Runs unit tests in dev worktree (`-m "not integration"`) as a quick gate
- Merges dev → test (server auto-reloads via `--reload`)
- Waits for health check at `http://127.0.0.1:8766/health`
- Runs the full test suite (unit + integration) from the test worktree against the live server

If any step fails, stop and report the failure. Do not proceed to step 3.

### 3. Ship test → main

```bash
~/workspace/claude-hooks/scripts/deploy.sh --ship
```

This merges test → main. No tests run here — they already passed in step 2. The script first checks `git log test..main` and warns if main has commits test doesn't (task:5e2a3216 — a sign main was edited out-of-band, e.g. skills committed direct-to-main; that once caused a real merge conflict here). A warning doesn't block the merge — it just gives context before a possible conflict instead of a bare "CONFLICT" with none. If a conflict does occur, resolve it the same way as any merge: read each hunk, confirm which side is the real update, then stage and commit.

### 4. Done

Report:
```
✓ Deployed to main.
  Unit gate:   passed (dev)
  Full suite:  passed (test)
  main is now at: <git log --oneline -1 ~/workspace/claude-hooks>
```

## Rules

- Never skip the unit gate or full suite — don't pass `--no-verify` or comment out test steps.
- If the health check fails after merge, stop — the server didn't reload cleanly. Ask the user to check the server process.
- If integration tests fail, stop and report which tests failed. Do not ship to main.
- This skill only applies to the `claude-hooks` project (worktrees at `~/workspace/claude-hooks-dev`, `~/workspace/claude-hooks-test`, `~/workspace/claude-hooks`).
