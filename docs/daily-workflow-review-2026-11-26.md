---
tags: claude-hooks, taskfw, cli-graph, stock-tracker-mcp, daily workflow, efficiency review, memory injection noise, tmux nudge, prompt shadow, commit linking
---
# Daily workflow review: hooks with taskfw, cli-graph, stock-tracker-mcp (2026-11-26)

task:ead42e9d. No code changed. Windows: `claude_hooks.sqlite` from 11-23 16:47 UTC
to 11-26 (~2.5 days); memory audit, `mac cli_log` and transcripts over 3 days.
Counts include development and test sessions, not only market work.

## Verdict

The pipeline is reliable (4 client failures logged, 2 of them UserPromptSubmit
timeouts) and cheap on the server. The waste is in what it adds: memories on
prompts that are not questions, a reminder that mostly misfires, and a finished
experiment that still runs on prompts.

## Findings, ranked

| # | Finding | Evidence | Fix |
|---|---------|----------|-----|
| 1 | Memory injection ignores prompt kind | 604 prompts, 1539 injections. Agent/cross-session/notification: 70 prompts, 207 memories. `/lastresponse`, `/handoff`: 49 prompts, 9. Prompts of 4 words or fewer: 124 prompts, 295. Other slash: 61 prompts, 180 | Skip machine messages and pass-through slash commands; require a stronger score for very short prompts |
| 2 | Same memory re-injected within a session | 220 of 1539 injections | Skip names already injected this session (reset after compaction) |
| 3 | Stale memories are injected as fact | `claude-hooks-recency-pull` points to `claude-hooks-open-work`, which does not exist; `taskfw-and-claude-hooks-logs-past-context` names `mcp__claude-hooks__*` tools that no session has. Both injected on the first prompt of the review session. An ORCL earnings memory was injected for "do the missign review" | Edit or delete the two; check `[[links]]` and `mcp__` names in a memory audit |
| 4 | tmux reminder mostly false positives | First match per session over 3 days: 31, of which 24 are `watch` (the sampled ones are JSON keys or variables in market scripts, `watch_next` style data), 4 `http.server`, 3 other. 85 reminder attachments in transcripts | Match command heads only; drop bare `watch` |
| 5 | The find_cli prompt shadow still runs | 7 days: 523 prompts followed by a `mac` call, 37 hits (7%). Cost per prompt: median 34 ms, p95 455 ms, max 1910 ms. The 2026-11-01 design rules out a prompt-time pre-lookup | Remove the shadow, with a tombstone |
| 6 | `mac git commit` is not linked to its task | `taskfw.commit_hook.commit_dirs('mac git commit -m "x task:abcdef12"')` returns `[]`; plain `git commit` is detected. cli-graph's git group has no `add_commit` call found by grep. Usage is low (80 `git commit` vs 3 `mac git commit` line-starts in transcripts) | Teach the commit hook the `mac git commit` form |
| 7 | Commit gate rejects `-F "$S/msg"` | 3 blocked calls in transcripts built the message path from a shell variable; 2 denies in the log window | Already explained in the deny reason. Put "pass -F a literal path" in the `/commit` skill |
| 8 | Tests write into production logs | `zz-no-such-memory` (cli-graph `tests/test_parity_hooks.py:176`): 23 warnings in the hook log, 6 rows in the 3-day `mac` call log | Point the test at a temp DB and log |
| 9 | Two files named `tool_hints.sqlite` | `~/.claude/` holds usage counts, shadow and find_cli logs (its `mcp_tool_calls` stops at 09-03); the iCloud copy holds the live `mac` call log. Global CLAUDE.md lists only the iCloud one as "tool usage hints" | Fix the CLAUDE.md line; drop the dead table |

Smaller: injected headers still end in `[]` (11-23 review #9); live-session
notices are logged at WARNING and bury real warnings.

## Cost

| Item | Figure |
|---|---|
| UserPromptSubmit, server route | median 190 ms, p95 833 ms, max 3200 ms; client limit 2000 ms |
| Injected context per prompt | median ~1278 tokens, p95 ~1944; memories ~796 of the median |
| PreToolUse / PostToolUse, server | median 38 / 25 ms |
| Client process per hook call | ~130 ms (3 runs); taskfw commit hook ~80 ms more per Bash call |

## Working well

- Commit traceability: shape gate (claude-hooks), the check inside `mac git commit`, and linking (taskfw) have separate owners.
- find_cli usage counts come from the hooks' PostToolUse counters (`cli-graph/find_cli/usage.py`).
- Vault reindex after writes (68 log lines) and the WebSearch cache (4 hits).

## Not measured

- Whether injected memories get used. Only 30 audit verdicts exist, too few and not a random sample.
- Any stock-tracker-specific friction: the logs were not split by repo or skill.
- `mac kitty` failures cluster on 11-25 across run/notify/schedule/comm_send; they look like development of those verbs and were not treated as daily friction.
- Context-size reminder (101 attachments in 3 days): whether it changes behaviour.

## Suggested order

4 and 5 (small deletions), then 1 and 2, then 3 and 6. The rest when nearby.
