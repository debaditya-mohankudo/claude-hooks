---
name: what-am-i-working-on
description: Show recent activity from server memory — prompts, tool calls, and task activations across sessions — plus today's vault notes created, edited or read. Use when asked "what was I working on?" or invoked as /what-am-i-working-on.
user-invocable: true
updated: 2026-11-06
model: haiku
---

Canonical home: this repo (claude-hooks), because the skill is primarily about
claude-hooks' own systems — server_memory.sqlite and the /session/live
endpoint (both owned by hooks/server.py). taskfw logs are deliberately
not fetched: tasks are driven by skills, whose activity already appears in
the event log. ~/.claude/skills/what-am-i-working-on/ is a deployed
copy, not a second source of truth; edit here and re-copy, not the other
way round. Do not fork a copy into task-framework or any other repo.

## Intent

Quick cold-start orientation tool. Fetches the last 50 events from the hook server's unified event log, which `claude` CLI processes are actually live right now, and today's vault file activity, and presents all three as a summary.

## How to use this skill

1. Run `mac hooks server_memory --n-events 50` — event log (prompts, tool calls, task activations). No transformation needed — it returns the same formatted markdown table, printed as a JSON-quoted string (literal `\n`), which reads fine as-is.

```bash
mac hooks server_memory --n-events 50
```

2. Fetch live session info from the hook server's `/session/live` endpoint (OS-level `ps`-based check for running `claude` CLI processes — catches stale/forgotten sessions that the event log alone won't show):

```bash
curl -s http://127.0.0.1:8766/session/live
```

Present it as: count of live sessions, and for each one its pid and elapsed time (`etime`).

3. List today's Obsidian vault activity (notes created, edited, or logged as read) from the vault file log. The event log does not record Read/Edit/Write calls, so it cannot show which notes changed; this log does, including notes made in Obsidian or other apps:

```bash
mac vault_log scan >/dev/null && mac vault_log recent --days 1 --kind all
```

`--days 1` means today only. Present it as a table of time, event (created / edited / read) and vault path, grouped by event. An empty list means no vault activity today; say so.

If the event log or the live-session call fails (the event log command returns an error, or the curl fails/connection refused), report that the hook server is unreachable and suggest checking `launchctl list | grep claude-hooks`. If `mac vault_log` fails, report that on its own; it does not mean the hook server is down.
