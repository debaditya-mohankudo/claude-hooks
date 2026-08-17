---
name: what-am-i-working-on
description: Show recent activity from server memory — prompts, tool calls, and task activations across sessions. Use when asked "what was I working on?" or invoked as /what-am-i-working-on.
user-invocable: true
updated: 2026-08-17
model: haiku
---

## Intent

Quick cold-start orientation tool. Fetches the last 50 events from the hook server's unified event log, plus which `claude` CLI processes are actually live right now, and presents both as a summary.

## How to use this skill

1. Call `mcp__claude-hooks__hooks__server_memory` with `n_events=50` — event log (prompts, tool calls, task activations). No transformation needed — the tool returns a formatted markdown table.

```python
mcp__claude-hooks__hooks__server_memory(n_events=50)
```

2. Fetch live session info from the hook server's `/session/live` endpoint (OS-level `ps`-based check for running `claude` CLI processes — catches stale/forgotten sessions that the event log alone won't show):

```bash
curl -s http://127.0.0.1:8766/session/live
```

Present it as: count of live sessions, and for each one its pid and elapsed time (`etime`).

If either call fails (event log tool returns `{error: ...}`, or the curl fails/connection refused), report that the hook server is unreachable and suggest checking `launchctl list | grep claude-hooks`.

3. Fetch the last 10 `task-*` skill invocations (which task-skill, i.e. task-grooming/task-implementation/task-introspection, fired most recently — a different signal than the event log's tool-call history):

```python
mcp__taskfw__tasks__logs(limit=200)
```

`logger` has no prefix query, so this over-fetches and filters client-side (same approach as the `task-skill-logs` skill): keep rows whose `logger` starts with `taskfw.skill.`, take the most recent 10 (rows are already most-recent-first), and present as timestamp / skill name (prefix stripped) / task_id / message. If fewer than 10 skill-invocation rows turn up in the 200 fetched, say so rather than padding; if the `mcp__taskfw__*` tools aren't available in this session, skip this section silently rather than failing the whole summary.
