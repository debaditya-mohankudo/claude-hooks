---
name: what-am-i-working-on
description: Show recent activity from server memory — prompts, tool calls, and task activations across sessions. Use when asked "what was I working on?" or invoked as /what-am-i-working-on.
user-invocable: true
updated: 2026-06-22
---

## Intent

Quick cold-start orientation tool. Fetches the last 50 events from the hook server's unified event log and presents them as a chronological summary.

## How to use this skill

Call `mcp__claude-hooks__hooks__server_memory` with `n_events=50`, then present the result directly to the user. No transformation needed — the tool returns a formatted markdown table.

```python
mcp__claude-hooks__hooks__server_memory(n_events=50)
```

If the tool returns `{error: ...}`, report that the hook server is unreachable and suggest checking `launchctl list | grep claude-hooks`.
