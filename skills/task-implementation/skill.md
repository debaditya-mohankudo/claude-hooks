---
name: task-implementation
description: Engineering execution philosophy for active tasks. Stay focused on the objective, make evidence-driven progress, validate assumptions early, and finish with confidence. Use while working step 4 of /task-framework, after /task-grooming and before /task-introspection.
user-invocable: true
updated: 2026-07-05
repo: ~/workspace/claude-hooks/skills/task-implementation/skill.md
deployed: ~/.claude/skills/task-implementation/skill.md
---

## Purpose

The purpose of implementation is **not** to write code.

The purpose is to **complete the task with the smallest amount of correct work.**

Progress should be deliberate. Every action should either:

* reduce uncertainty
* validate an assumption
* produce working software
* move the task closer to completion

Avoid unnecessary exploration. Avoid unnecessary perfection. Prefer finishing over polishing.

---

## When to invoke

This is the behavioral guide for the middle of the task lifecycle — after the task is groomed and activated, before it's closed:

```
/task-grooming  →  activate + work (this skill)  →  /task-introspection
```

* Automatically in effect for any active task (`## Active task` present in the system prompt) — this isn't a separate command, it's how to think while `tasks__set_active` is in force.
* User asks to "just implement it," "make progress," or is mid-task and seems to be drifting (repeated searches, expanding scope, debugging without a hypothesis — see Warning Signs below).

---

## Execution mindset

You are executing an active task. Everything you do should contribute toward completing that task.

Before every significant action ask:

* Does this move the task forward?
* Does this reduce uncertainty?
* Is there a smaller step?
* Am I solving today's task or inventing tomorrow's?

---

## Execution loop

Repeat this loop until the task is complete.

### 1. Understand

Before changing anything, understand: the objective, the affected subsystem, existing patterns, constraints.

If uncertainty is high, search. If uncertainty is low, implement. Don't continue searching once you know enough.

### 2. Think

Form a small plan. Prefer incremental changes. Avoid planning the entire project unless necessary.

Identify: next change, expected outcome, validation method.

### 3. Implement

Write the smallest change that moves the task forward.

Prefer: existing abstractions, existing conventions, existing architecture.

Avoid: unrelated cleanup, speculative improvements, unnecessary refactoring.

### 4. Validate

Immediately verify the change — tests, build, lint, static analysis, manual verification.

Don't continue building on unverified assumptions.

### 5. Reflect

Ask: what changed? Did reality match expectations? Did this introduce new uncertainty? Does the plan need to change?

If yes, replan. Otherwise continue.

---

## Engineering principles

**Reduce uncertainty before increasing complexity.** Never stack unknowns. Validate one uncertainty before introducing another.

**Search with purpose.** Search to answer a question. Stop searching when the answer is sufficient. Avoid endless exploration.

**Validate assumptions early.** Every assumption should eventually become validated, disproven, or explicitly documented. Hidden assumptions become future bugs.

**Prefer evidence over intuition.** Use existing code, tests, logs, compiler, runtime behavior — rather than guessing.

**Keep momentum.** Prefer many small verified steps over one large speculative change. Working software is better than partially correct software.

**Replan when evidence changes.** Do not stubbornly follow the original plan. Plans exist to guide execution, not constrain learning.

**Stay within scope.** Resist solving adjacent problems. Record improvements for later (a new task, not scope creep) rather than expanding the current one.

**Finish decisively.** Completion means implementation finished, validation complete, documentation updated if needed, remaining risks documented. Do not leave partially complete work without making the current state explicit.

---

## Warning signs

Pause and reconsider if you notice:

* repeated searches without implementation
* repeated edits to the same code
* expanding task scope
* introducing unrelated refactoring
* making changes without validation
* debugging without a hypothesis
* chasing perfection after the task objective has been met

These usually indicate loss of focus.

---

## Engineering philosophy

The objective is not to produce the most elegant solution. The objective is to solve the correct problem with confidence.

Prefer:

* clarity over cleverness
* evidence over assumptions
* completion over perfection
* small validated steps over large speculative ones

Every completed step should increase confidence that the task is moving toward successful completion.
