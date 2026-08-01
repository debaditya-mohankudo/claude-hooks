---
description: Gracefully pause mid-session — save pending intent to active task, then wait for user input.
---

# /pause

Stop after the current action completes, save any pending intent to the active task, and explicitly wait for the user.

## When to invoke

- User says `/pause`, "pause", "wait", "hold on", or similar mid-session redirect signal
- User wants to provide input before Claude continues to the next step
- Claude is about to start a multi-step plan and user wants to review before proceeding

## Behavior contract

**Never abort mid-tool-call.** Finish whatever tool call is in flight, then pause.

### Step 1 — Identify pending intent

What was Claude about to do next that hasn't happened yet? Capture it as a short list:
- Planned tool calls not yet made
- Reasoning that was in progress
- Next steps in an active implementation plan

If nothing is pending (Claude was idle), skip to Step 3.

### Step 2 — Save to active task (if one exists)

If there is an active task (visible in `## Active task` in the system prompt):

```python
mcp__claude-hooks__tasks__pause(
    task_id="<active_task_id>",
    pending=["<item 1>", "<item 2>"],
    session_id="<session_id from Turn state>"
)
```

Keep the list under 5 items — quick-scan reminder, not a transcript. The tool formats the `## Pending before paused` section and overwrites any previous pause state.

If no active task exists, skip this step.

### Step 3 — Signal pause to user

Output exactly this pattern (adapt wording, keep the structure):

```
Paused. [One sentence: what was just completed.]

Pending (saved to task:<id>):
- <item 1>
- <item 2>

Waiting for your input.
```

If no active task: omit the "saved to task" parenthetical.

### Step 4 — Stop

Do not propose next steps. Do not ask clarifying questions. Do not continue reasoning. End the response after the pause signal. The user will continue when ready.

## Example

User invokes `/pause` while Claude is mid-plan implementing a feature:

```
Paused. Finished reading load_memories.py.

Pending (saved to task:4a2b1c3d):
- Implement two-query split (always-include vs scored batch)
- Update tests for new SQL shape

Waiting for your input.
```

## Notes

- The `## Pending before paused` section in the task body is overwritten on each `/pause` — it reflects the most recent pause state, not a log.
- This skill does not close or deactivate the task. The task remains active and its history continues after the user resumes.
- If you were in the middle of a `/task-framework` decomposition step, note which step was reached.
