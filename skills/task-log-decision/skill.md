---
name: task-log-decision
description: Log an explicit design decision for the active task. Persists it to task_events and appends to mid_task_decisions in the checkpoint so it is injected every subsequent turn. Use when the user says "log this decision", "remember this choice", or invokes /task-log-decision.
user-invocable: true
updated: 2026-06-11
---

# /task-log-decision

Log a load-bearing design decision for the active task so it persists across all remaining turns and future sessions.

## When to invoke

- User says "log this decision", "remember this choice", "note this"
- User explicitly invokes `/task-log-decision`
- A significant architectural or design choice was just made that affects future work

## Steps

### 1. Determine target task and session

**Explicit task ID** — if the user passed `task:<id>` as an argument (e.g. `/task-log-decision task:8366ad16 <text>`), use that ID.

**Active task fallback** — otherwise read from `## Active task` in the system prompt. If neither is present, tell the user and stop.

Session ID always comes from `## Turn state`.

### 2. Compose the decision text

Everything after the optional `task:<id>` argument is the decision text — use it verbatim.
If no text was provided, summarise the decision in one line: **what was chosen and why** (rationale is the most important part).

Good: "Chose opaque tokens over JWT — avoids key rotation complexity on short-lived sessions"
Bad: "Using opaque tokens"

### 3. Call tasks__add_decision

```python
mcp__claude-hooks__tasks__add_decision(
    task_id="<target_task_id>",
    decision="<one-line decision with rationale>",
    session_id="<session_id>"
)
```

### 4. Confirm

Reply: `Decision logged to task:<id>: "<decision text>"`

The decision will appear under `## Task decisions` in every subsequent turn's system prompt for that task.
