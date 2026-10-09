# kitty session dashboard

Optional. A read-only live view of `~/.claude/server_memory.sqlite`: latest task
activation plus the last N prompt/tool events. Stdlib only; nothing here is
imported by the hooks, so claude-hooks works unchanged without kitty.

    kitten @ launch --location=vsplit --keep-focus python3 ~/workspace/claude-hooks/kitty/dashboard.py

Needs remote control (`allow_remote_control socket-only` + `listen_on`). Optional
keybinding in `kitty.conf`:

    map f1 launch --location=vsplit --keep-focus python3 ~/workspace/claude-hooks/kitty/dashboard.py

Flags: `-n` events (15), `-i` refresh seconds (2), `--once` print and exit.
`CLAUDE_SERVER_MEMORY_DB` overrides the DB path.

## Claude -> pane note

Whatever is in `~/.claude/dashboard_note.md` (override: `CLAUDE_DASHBOARD_NOTE`)
is shown at the top, up to 12 lines. Claude overwrites it with its current plan
or what it is waiting on; delete the file to clear it. Costs no prompt context.

## Hook-mirrored plan

`hooks/dashboard_plan.py` (called from `client.py` on PostToolUse) writes
`~/.claude/dashboard_plan.json` (override `CLAUDE_DASHBOARD_PLAN`; `=0` disables)
and the dashboard shows it as `PLAN done/total` with `[x]`/`[ ]` items:

- `TodoWrite` replaces the plan with the todo list
- taskfw `tasks__create` sets title + resolution checklist
- taskfw `tasks__check_item` ticks the item, if the plan is that task's

This file is hook-owned; `dashboard_note.md` stays Claude's for free text
(e.g. "waiting on you for X").

## Turn tab + next-steps split

`kitty/layout.sh` opens a tab with `--view turn` (current prompt and the final
summary of the last turn) and a parallel vsplit with `--view next` (pending plan
items only). `hooks/dashboard_turn.py` feeds it on UserPromptSubmit / Stop via
`~/.claude/dashboard_turn.json` (`CLAUDE_DASHBOARD_TURN`; `=0` disables).
