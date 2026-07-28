---
name: task-grooming
description: Pre-implementation grooming pass. Reduce uncertainty before implementation by activating the task, gathering related context, identifying hidden assumptions, and improving task readiness. Use before starting a task or sprint. Invoke with /task-grooming, /task-grooming task:<id>, or /task-grooming epic:<id>.
user-invocable: true
updated: 2026-07-27
repo: ~/workspace/claude-hooks/skills/task-grooming/skill.md
deployed: ~/.claude/skills/task-grooming/skill.md
---

## Purpose

The purpose of grooming is **not** to make a task prettier.

The purpose of grooming is to remove uncertainty before implementation.

After grooming, an engineer should know:

* **What** to build.
* **Where** to build it.
* **Why** it should be built this way.
* **What risks remain.**
* **What success looks like.**

A well-groomed task should allow implementation to begin immediately without another planning pause.

---

## When to invoke

* Before starting implementation.
* Before activating a task for development.
* After creating an epic and its subtasks.
* Whenever a task has significantly changed in scope.

---

## Input resolution

| Invocation | Action |
|---|---|
| `/task-grooming` | List open tasks and ask which to groom |
| `/task-grooming task:<id>` | Groom one task |
| `/task-grooming epic:<id>` | Groom all open/blocked children of that epic |
| `/task-grooming task:<id1> task:<id2>` | Groom explicit list |

```python
# Single task
mcp__claude-hooks__tasks__get(id="<id>")

# Children of an epic
mcp__claude-hooks__tasks__list()  # filter by parent_id == epic_id
```

If no argument given, call `tasks__list()` and ask which tasks to groom.

---

## Step 1 — Activate and read context

```python
mcp__claude-hooks__tasks__set_active(task_id="<id>", session_id="<session_id>")
```

**Never guess the session_id.** Read it from the `## Turn state` system-prompt block when visible. If it isn't visible, use `mcp__claude-hooks__hooks__session_id` (built-in retry, no active task required) rather than inventing one.

Activation is mandatory because it retrieves:

* Active task (body, decisions)
* Related tasks (top-3 semantically similar)
* Related commits (top-3 diff hunks)
* Code RAG (top-3 modules)
* Concept store matches, if the repo has one (see Step 2)

These are the primary inputs to grooming — reading the body in isolation without activating is not grooming.

**Read the existing `document.grooming` as your starting draft, not a blank page.** `tasks__get`'s response already includes `document.grooming` (clarifications/hidden_assumptions/risks/prior_art/suggested_improvements from the *last* pass, if any). Re-grooming still overwrites this namespace wholesale (task:74dad096 — no version history is kept, and the user has confirmed that's the intended design), but the CONTENT you write in Step 6 should be an edited revision of what's already there: carry forward items that are still accurate, revise ones that have changed since, drop ones now resolved or irrelevant, and add this pass's new findings. Writing a fresh grooming block from scratch while ignoring an existing one throws away real signal (e.g. a risk graded `avoided` or `materialized` at a prior introspection is evidence about what actually held up) and risks re-flagging something already settled.

**Grooming a large batch (>5–10 tasks, e.g. a big epic):** the literal per-task activate → wait-a-turn → read-injected-context loop doesn't scale — each activation's related-context only lands on the *next* turn, so 20+ tasks means 20+ turns. When batch size crosses that threshold, it's acceptable to substitute direct lookups for equivalent signal instead: `tasks__get` on all candidates up front, `tasks__neighbors`/`diff_rag__query`/`code_rag__smart_search` called directly rather than waiting for injection, and grepping the actual repo for files named in each task's `Files:` section to verify claims. State plainly in the report that this substitution was made and why — it's a disclosed deviation, not silent corner-cutting.

---

## Step 2 — Concept store lookup (if the repo has one)

Two formats are in active use (corrected 2026-07-24 — this step previously only checked the JSON format and silently skipped every SQLite-format repo, e.g. SeniorDevAgent, treating "wrong format" as "no store exists"). Detect first, same check `/update-concept-store` uses:

```bash
test -f "<repo>/concept_store/concepts.json" -a -f "<repo>/concept_store/store.py" && echo json
test -f "<repo>/concepts.db" -a -f "<repo>/concept_store.py" && echo sqlite
```

**JSON format** (claude-hooks-dev pattern):

**Always use `concept__list(repo="<repo>")`/`concept__get(repo="<repo>", name=...)` (task:2813ece5) — never hand-parse `concept_store/concepts.json` directly.** Its top-level shape is `{"concepts": [...], "meta": {...}}` — a list under a `concepts` key, not a flat name-keyed map. A `json.loads(...).values()` script silently matches nothing against this shape. This bug independently missed a real, badly-stale match twice in one session (task:da29c842's own grooming/introspection pass, and `/deploy`'s concept audit step) before being caught by chance — don't repeat it. This applies to claude-hooks-dev's own store too, not just non-Java target repos.

Prefer a `Concepts:` section in the task body if present — look those slugs up directly via `concept__get`. Otherwise match the task's `Files:` section against each concept's `module` field from `concept__list`'s output.

**SQLite format** (SeniorDevAgent pattern):

```python
from concept_store import ConceptStore
store = ConceptStore("<repo>/concepts.db")
concepts = store.list_concepts()  # then match by domain, or by evidence source_ref against Files:
```

Match the task's `Files:` section against each concept's evidence (`store.get_evidence(concept_id)`'s `source_ref` values) or against `domain`/`name` for the subsystem a touched file belongs to — same matching logic `/update-concept-store` Step 2b already uses.

For each match, in either format, check:

* **Invariant conflict** — does the task's plan violate something the concept's description asserts as always-true for that module? (SQLite concepts don't have a separate `invariants` field — read this out of `description`, and check `risk_findings`/`test_gaps` via `get_risk_findings`/`get_test_gaps` for already-known open concerns the plan should account for.)
* **Contract break** — does the plan change what the module promises callers?
* **New concept** — does the task introduce behavior not captured by any existing concept?

Append matches as a `## Concept context` block in the grooming notes:

```
## Concept context
- hooks/gates.py: gates-prereq-chain-enforcement
  invariants: ["Gates fail open on DB errors", "External gates never override internal ones"]
  → check: does this task's change respect these invariants?
```

(SQLite format: substitute the concept's `name`/`domain` and relevant `description`/open risk_findings for the JSON example's `module`/`invariants`.)

Skip silently only if NEITHER format's store files are present — not just because the JSON path specifically didn't match.

---

## Step 2b — Repo memory lookup (if the repo has migrated to the split store)

Repo-specific `project`/`reference` memories about this repo's own code/architecture live in a committed per-repo JSON store (`repo_memory/memories.json`), separate from the global `MEMORY.sqlite` — see task:850ddd65. Not every repo has migrated yet; treat absence the same as concept_store's absence, a normal state, not a gap to flag.

```bash
test -f "<repo>/repo_memory/memories.json" && echo has-repo-memory
```

If present:

```python
mcp__claude-hooks__repo_memory__list(repo="<repo>")
```

Match the task's `Files:` section against each memory's `files` field (comma-separated, same stem-based matching convention `activate_task.py`'s task-activation lookup already uses) — no need to hand-roll a different heuristic here.

Append matches as a `## Repo memory context` block in the grooming notes, parallel structure to `## Concept context`:

```
## Repo memory context
- dispatcher-is-table-driven (type: project)
  body: "Adding a new tool family is one DOMAIN_MAP entry + one tools/<name>.py module."
  → check: does this task's plan account for this known fact?
```

For each match, ask the same question `## Concept context` asks of invariants: does the task's plan conflict with, or fail to account for, what this memory already establishes as true about the repo? Unlike concepts (structured invariants/contracts), a repo memory is freeform prose — there's no formal conflict check, just a judgment call on relevance.

Skip silently if `repo_memory/memories.json` doesn't exist for this repo.

---

## Step 3 — Read before judging

Before auditing, read all injected/gathered context completely. Then ask:

1. What does this context confirm?
2. What does this context change?
3. What uncertainty has disappeared?
4. What uncertainty still remains?

The goal is not to collect more information — it's to determine whether enough now exists to implement confidently.

---

## Step 4 — Engineering review

### 1. Is the outcome obvious?
If two engineers independently completed this task, would they likely produce essentially the same implementation? If not, identify the ambiguity and recommend a clarification.

### 2. Can implementation begin immediately?
If not today, identify the missing information, the blocking decision, or the missing dependency.

### 3. Are assumptions hidden?
Look for assumptions that exist only in the author's head — architecture, API behavior, data format, ordering, deployment expectations. Validate what can be validated now; record the rest explicitly.

### 4. Is the task's own stated premise verified, not just assumed?
For infra/plumbing tasks especially — anything claiming "X is the authoritative file," "Y calls Z," "this is the production path," "this duplication is accidental drift" — spend one concrete verification step (git log/git show on the relevant file's history, grep for actual callers, live inspection of the running DB/service) before accepting the task body's framing at face value. Don't just implement what the task description says needs to happen. This caught three real bugs in one session (task:4b5bf21f: task description didn't mention 5 internal callers, only caught by the integration suite; task:46634a19: task named the wrong file for the actual production fix; task:9d3acbef: the task's entire premise — that a duplication was accidental — was wrong, and investigating why surfaced a separate real bug). See memory `verify-production-path-before-accepting-task-premise`.

### 5. Does historical context change the plan?
Review related tasks, related commits, code RAG, memories. Would you implement this differently after reading them? If yes, record it as a grooming note.

### 6. Is this task a duplicate or orphan?
Check it against its parent and siblings, not just unrelated related-tasks matches: does it restate the parent epic's own vision instead of a concrete piece of it? Does its `parent_id` actually match what its tags claim? Is its `project:` tag consistent with its siblings? Duplicate/orphan tasks are cheap to create by accident (parallel task creation, copy-paste) and expensive to leave live — they fragment ownership and waste future grooming passes. Flag explicitly, don't fold into a generic "conflicts" note.

### 7. Is the task appropriately sized?
Can this reasonably be completed in one focused implementation session? If not, recommend splitting into smaller subtasks.

### 8. What is most likely to stall implementation?
Predict the largest remaining risk — hidden coupling, unclear ownership, missing design decision, unknown API, migration uncertainty, missing tests. Record it.

This prediction is graded at introspection time (`/task-introspection` Step 3.0: materialized / avoided / wrong / missed), so state it concretely and falsifiably — "choosing the UPS injection mechanism will stall" can be graded; "there may be unknowns" cannot.

---

## Step 5 — Structural validation

Deterministic checks, run after the engineering review:

| Check | Pass condition | Flag |
|---|---|---|
| **Resolution format** | `Resolution:` exists and is a checklist (`- [ ]`) | "prose — convert to checklist" |
| **File paths named** | Each checklist item names a concrete file/module/subsystem | "file paths missing" |
| **Dependencies stated** | If this task needs another first, it's noted | "dependency on X not stated" |
| **Related task conflicts** | No related task contradicts this plan | "conflicts with task:<id> — <what>" |
| **Duplicate ownership** | No other task's checklist independently tracks the *same file edit* this task owns | "duplicates task:<id> on <file> — consolidate ownership" |
| **Prior art reused** | Related tasks/commits surface relevant existing patterns | "note prior art from task:<id>" |
| **Design decisions deferred** | No "TBD" where a concrete decision is needed to start | "decision needed: <what>" |
| **Concept invariant respected** | Plan does not violate stored invariants for touched files | "invariant risk: <module> — <invariant>" |
| **Checklist/status mismatch** | If every Resolution item is `[x]`, status is not left `open` | "all items checked but status is open — finish or explain what's still blocking" |

Duplicate ownership is distinct from a contradiction: two tasks can agree on *what* to do to the same file and still be a problem, because neither is the source of truth for when it's done. Consolidate to one canonical owner and have the others defer to it (link `relates_to`/`depends_on` via `tasks__link_tasks`), rather than leaving the same checkbox in two places.

---

## Step 6 — Update the task

Grooming output is written through `tasks__update_document` as structured data (epic:f42b6958, adopted per task:2f275e17) — not appended as a `## Grooming Notes` markdown section to the body.

Do **not** rewrite the body. Instead:

```python
mcp__claude-hooks__tasks__update_document(
    id="<task_id>",
    grooming={
        "clarifications": ["..."],
        "hidden_assumptions": ["..."],
        "risks": [{"text": "...", "graded": None}],
        "prior_art": ["..."],
        "suggested_improvements": ["..."],
    },
    related={"concepts": ["<concept-slug>", "..."]},  # only if Step 2 found concept matches
    mark_groomed=True,
)
```

`grooming` REPLACES `document.grooming` wholesale — only the latest pass is kept, `last_run_at` is set server-side. This is intentional (task:74dad096, confirmed 2026-07-26: no version history is wanted for grooming passes). The wholesale replace is about STORAGE, not AUTHORING: the `grooming` object you pass here should be built by editing the existing `document.grooming` you read in Step 1 as a draft (carry forward, revise, or drop each field), not written fresh while ignoring it. If something from the prior pass is worth keeping verbatim as historical context rather than as a live finding, put it in `prior_art` explicitly — that field is the one place a prior pass's content survives unedited across re-groomings.

`related.concepts` is additive (extended + deduped, never overwritten) — pass only the concept slugs *this* grooming pass found; `/task-introspection`'s Step 5 adds its own findings to the same list independently, without erasing what grooming wrote.

`mark_groomed=True` (task:5e2a3216 — folded into `tasks__update_document` itself, no longer a separate `tasks__update` call) sets the task's `groomed_at` timestamp — the structured signal that grooming ran (task:46634a19). Always pass it, even when the grooming findings are minimal, so `tasks__list`/`tasks__get` can surface "groomed" without a body substring search. Compare `groomed_at` against `updated_at` to detect staleness: if the body was edited after the last groom, treat the task as no longer confidently groomed.

If a duplicate/ownership consolidation was found, also call `tasks__link_tasks(from_id, to_id, relation_type="duplicates"|"depends_on"|"relates_to")` to record it structurally, not just in prose.

If a task looks like a duplicate/orphan warranting `abandoned` status rather than a note, don't decide unilaterally — surface it to the user (e.g. via a clarifying question) before changing status.

If no changes are required, still call `tasks__update_document(id="<task_id>", grooming={...}, mark_groomed=True)` with the (possibly near-empty) findings to record that grooming ran, and note "ready as-is" in the report.

---

## Step 7 — Reset status

If activation changed the task's status, restore it to `open`:

```python
mcp__claude-hooks__tasks__update(id="<task_id>", status="open")
```

Grooming is preparation, not execution.

---

## Step 8 — Report

Per task:

```
✓ task:abc — ready  (title)
⚠ task:def — 2 gaps: file paths missing, decision needed: storage format  (title)
⚠ task:ghi — duplicates task:xyz on tools/db.py  (title)
```

Then a summary line:

```
N tasks groomed — M ready, K need updates.
```

If gaps were found: "Fix the flagged items and re-run `/task-grooming` before activating."

---

## Rules

- **Activation is mandatory** for anything below batch-size threshold (see Step 1). Related-task and diff-RAG context is only injected when a task is active — reading the body in isolation is not grooming.
- **Reset to open after grooming.** A groomed task is not a started task.
- **Don't rewrite the body for grooming notes — they live in `document.grooming`, not the body.** Body edits are still fine for fixing the checklist/Files/Motivation itself (Step 5's structural fixes), just not for appending findings.
- **Treat the existing `document.grooming` as a draft to revise, not a blank page.** Read it in Step 1, edit it in Step 6. See Step 1's note.
- **Never guess the session_id.** Read from `## Turn state`, or use `hooks__session_id` if it isn't visible.
- **One task at a time below the batch threshold; disclose substitutions above it.** Don't silently skip activation for convenience — either do it, or say plainly that you didn't and why.

---

## Engineering philosophy

Every grooming pass should reduce uncertainty. Avoid editing merely for completeness. Instead ask:

* Does this make implementation easier?
* Does this eliminate a future planning pause?
* Does this reduce the chance of rework?
* Does this expose hidden assumptions?
* Does this make success more observable?

If the answer is no, the task probably does not need to change.

A successful grooming pass makes implementation boring. The engineer should be able to start coding immediately, with confidence, without needing another planning discussion.
