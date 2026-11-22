# kitty session dashboard

The viewer and layout live in cli-graph, not here:

    mac kitty dashboard [--view all|turn|next] [-n N] [-i SECS] [--once]
    mac kitty layout

Removed from this repo: `kitty/dashboard.py` and `kitty/layout.sh` (task:3f30d74d).
They are utility features, not hook logic, and hand-built `kitten @` calls in a
shell script repeated pitfalls `mac kitty` had already solved. Do not re-add a
second copy here.

What stays here is hook-driven and is the **interface** between the repos. The
viewer only reads these files; a rename or shape change on the writer side
blanks it silently, so the shapes are pinned in `tests/test_dashboard_contract.py`
(and from the reader side in cli-graph's `tests/test_kitty_dashboard.py`).
Change both together.

| File | Written by | Override (`=0` disables) |
|---|---|---|
| `~/.claude/dashboard_plan.json` | `hooks/dashboard_plan.py` on PostToolUse | `CLAUDE_DASHBOARD_PLAN` |
| `~/.claude/dashboard_turn.json` | `hooks/dashboard_turn.py` on UserPromptSubmit / Stop | `CLAUDE_DASHBOARD_TURN` |
| `~/.claude/dashboard_note.md` | Claude, free text (plan / what it is waiting on) | `CLAUDE_DASHBOARD_NOTE` |

Plan sources: `TodoWrite` replaces the plan; taskfw `tasks__create` sets title +
resolution checklist; `tasks__check_item` ticks the item if the plan is that
task's. The viewer also reads `~/.claude/server_memory.sqlite`
(`CLAUDE_SERVER_MEMORY_DB`).

`kitty.conf` and `tab_bar.py` stay in `kitty/config` (decision: moving them
gains nothing and adds a third location).
