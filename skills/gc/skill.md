---
name: gc
description: Git commit wrapper — stage all changes and commit with a derived message. Works on any git repo. Use when you need to commit code changes.
user-invocable: true
updated: 2026-07-27
wiki: "[[Documentation/Tools/SKILLS_WIKI#Developer Workflow Skills]]"
repo: ~/workspace/claude-hooks/skills/gc/skill.md
deployed: ~/.claude/skills/gc/skill.md
---

Git commit operations via `git_local.sh`.

## Intent

Designed for use during subtask workflows — commit each subtask without interrupting flow, then push manually once the parent task is closed. `/gc` never pushes; push is a deliberate end-of-task action.

## How to use this skill

When invoked directly (e.g. `/gc`), derive commit messages from session context — do not ask the user for them. Commit immediately without asking.

Always append a task id to the commit message body when one is available. Sources in priority order:
1. Explicitly passed as an argument: `/gc task:abc123` — use this id regardless of active task state
2. Active task visible in `## Active task` in the system prompt

Also append the parent epic id when the active task has a `parent:<id>` tag. Look it up via `tasks__get(id)` if not already known.

```
feat(area): short description

task:abc123
epic:def456
```

Omit the `epic:` line if the task has no parent.

## Grouping changes into multiple commits

When the session produced multiple distinct tasks, prefer splitting into one commit per task rather than one big commit. Use judgment — don't force a split when changes are tightly coupled.

**How to group:**
- Run `git status --short` and `git diff --stat HEAD` to see all changed files
- Infer task boundaries from file paths, layer (e.g. hooks vs server vs MCP tools), and what you know was done this session
- Propose the grouping to the user with one-line commit messages before committing
- Get confirmation, then commit each group with `git add <files> && git commit -m "..."`
- If `git_local.sh` is used, reset after the first commit if it grabs everything, re-commit in groups

**Guideline, not a rule:** If all changes belong to one coherent task, a single commit is fine. The goal is a readable history, not artificial splitting.

## Determining the target repo

Use the repo where the relevant changes were made — this is not always the primary working directory (`claude_for_mac_local`). Determine it from context:
- If the user just edited vault files → use `--repo ~/workspace/claude_documents`
- If changes are in the current project → omit `--repo` (uses CWD)
- If the user specifies a path explicitly → use `--repo <that path>`

## Running tests before commit

Before committing, check if a test suite exists in the repo and run it:

- `tests/` directory exists → run `uv run python -m pytest tests/ -q -m "not integration"` (only unit tests)
- If tests fail, report the failures and **do not commit** — ask the user how to proceed
- If tests pass, proceed with commit
- If no test suite found, skip and commit directly

## Committing changes (dry-run preview)

```bash
~/workspace/claude_for_mac_local/tools/git_local.sh [--repo <path>] "Your commit message here"
```

Shows: repo root, git status, staged/unstaged changes, commit message.

## Committing changes (confirmed)

```bash
~/workspace/claude_for_mac_local/tools/git_local.sh -y [--repo <path>] "Your commit message here"
```

## After committing

Confirm to the user: `✓ Committed: "Your commit message"`. Include the repo name when it's not the default project.

## Code graph refresh

After every successful commit, refresh the embeddings to keep them in sync with the new HEAD.

**For embeddings — incremental update of changed .py and .md files:**

The code RAG indexes Python source (by function/class) and `docs/` markdown (by section). Re-index whenever `.py` or `.md` files change.

Use the MCP tool if available (preferred — no subprocess overhead):
```python
changed = [f for f in subprocess.check_output(
    ["git", "diff", "--name-only", "HEAD~1", "HEAD"]
).decode().splitlines() if f.endswith(".md") or f.endswith(".py")]

if changed:
    mcp__claude-hooks__code_rag__index_files(files=changed)
```

Or via shell fallback:
```bash
changed=$(git diff --name-only HEAD~1 HEAD | grep -E '\.(py|md)$')
if [ -n "$changed" ]; then
    uv run python scripts/build_code_embeddings.py --files $changed
fi
```

If `.code_embeddings.tvim` does not exist yet, `--files` automatically falls back to a full rebuild.

Run silently — only surface output if errors. This keeps `meta.commit` current for forensic use and the RAG index fresh for `/explain`.

**For diff_rag — incremental update of the commit hunk index:**

After code_rag is updated, index the new commit into diff_rag:

```python
mcp__claude-hooks__diff_rag__index_commits(repo=".", since="HEAD~1", max_commits=1)
```

This embeds the diff hunks from the just-committed HEAD and appends them to `.diff_embeddings.tvim`. Skip silently if the index doesn't exist yet (it will be built on first full run of `scripts/build_diff_embeddings.py`).

If the script errors:
- Check the error message (e.g., "not in a git repository", merge conflicts)
- Suggest the user resolve any conflicts or check branch state

## Memory audit on changed files

After the code graph refresh, run a targeted memory audit against files touched by this commit. This is the same signal logic as `/memory-audit` but scoped to the commit rather than the full 10-per-run batch.

**Step 1 — get changed files:**
```bash
git diff --name-only HEAD~1 HEAD
```

**Step 2 — find memories whose `files` field overlaps:**
For each changed file path, search for memories that reference it:
```python
mcp__claude-hooks__memory__search(query="<changed_filename_stem>", domain="claude-hooks")
# e.g. for "hooks/gates.py" → search "gates"
# e.g. for "src/tools/memory.py" → search "memory tools"
```
Keep only hits where the memory's `files` field actually contains the changed path (partial match is fine: `gates.py` matches `hooks/gates.py`).

**Step 3 — check git signal on each matching memory:**
For each matching memory, get its full body via `memory__get` and run:
```bash
git -C ~/workspace/claude-hooks log --since="{memory.updated}" --oneline -- {memory.files}
```
If commits exist since `memory.updated` → the memory may be stale.

**Step 4 — surface stale candidates:**
Flag each stale hit to the user:
```
Memory `<slug>` may be stale — changed files: <files>, last updated: <updated>
Body: <first 100 chars>
```
Ask: "Update, validate, or skip?"
- **update** → user provides new body → `memory__add(name, body=<new>, ...)`
- **validate** → `memory__add(name, body=<same body>, ...)` — stamps `last_validated`
- **skip** → leave as-is

**Guidelines:**
- Only surface memories with `type=project` or `type=reference` — skip `feedback` and `user` type memories (they describe behavior, not code facts)
- Skip this step if no memories match the changed files
- Skip for trivial commits (test fixes, formatting, config tweaks with no logic change)
- If no `files` field on a memory → skip it (it's in the backfill queue for `/memory-audit`)

**Check docs:**
If the commit touches a file that has a corresponding section in `docs/`, note it: "Consider updating `<doc section>` to reflect this change."

## Hook server restart (claude-hooks repo only)

After code/diff RAG updates, if the committed repo is `claude-hooks`, restart the uvicorn hook server so it picks up the new code:

```bash
curl -s http://127.0.0.1:8766/health
```

The server does not run with `--reload`. Only check health — no restart needed unless deploy.sh is run. Report the result. Skip silently if the repo is not claude-hooks.

## Examples

**Commit in the current project:**
```bash
~/workspace/claude_for_mac_local/tools/git_local.sh -y "Fix authentication bug"
```

**Commit in the vault repo:**
```bash
~/workspace/claude_for_mac_local/tools/git_local.sh -y --repo ~/workspace/claude_documents "Add Index Terms to astrology notes"
```

**Commit in an arbitrary repo:**
```bash
~/workspace/claude_for_mac_local/tools/git_local.sh -y --repo ~/workspace/some-other-repo "Update config"
```
