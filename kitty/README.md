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

## "Is this session waiting on me?" — three signals, one vocabulary

Three different things answer that question, and until task:b5c1e08c two of
them disagreed about what a finished turn means. They are still written by
three independent writers — claude-hooks and taskfw are decoupled and neither
reads the other's signal (user decision 2026-11-03,
memory `use-mac-kitty-for-simple-kitty-ops`) — but the **words** now agree.

| Signal | Written by | Read by | Question it answers |
|---|---|---|---|
| tab colour + glyph | `hooks/kitty_status.py` (every tab) | your eyes; nothing programmatic | what is this session doing right now? |
| `state` kitty user var | taskfw `taskfw/kitty/hook.py` (task tabs only) | `taskfw-kitty list` | which task tabs are idle? |
| `~/.claude/dashboard_note.md` | Claude, free text (`WAITING you: …`) | `mac kitty dashboard` | what is it waiting *for*? |

Aligned event → state, with `waiting` meaning "the session wants the user".
The claude-hooks column is checked against the code by
`tests/test_kitty_status.py::test_readme_table_matches_event_state`; the taskfw
column is a copy of `taskfw/kitty/hook.py`'s `EVENT_STATE` as of task:b5c1e08c
and nothing here can check it — that file is the source of truth for it.

| event | claude-hooks (tab) | taskfw (`state` var) |
|---|---|---|
| SessionStart | waiting (`◆`, blue) | waiting |
| UserPromptSubmit | working (`●`, amber) | running |
| PreToolUse / PostToolUse | working (`●`, amber) | — |
| Stop | waiting (`◆`, blue) | waiting |
| Notification | needs_input (`▲`, red) | waiting |
| Notification `idle_prompt` | waiting (`◆`, blue) | waiting |
| SessionEnd | cleared | done |

`done` exists only in taskfw and only means *the session ended*; claude-hooks
expresses that by clearing the tab. claude-hooks keeps `needs_input` as a
louder, redder case of waiting (a permission prompt), which taskfw does not
distinguish because its one reader only cares whether the tab is idle.

### Alerts: one per turn, and they are not duplicates

| Alert | Where | Fires on | Scope |
|---|---|---|---|
| `afplay` chime | claude-hooks `langchain_learning/nodes/play_sound.py` (server-side, via `run_stop`) | `Stop` only, once per turn (`stop_alert_sent` / `sound_played`) | every session |
| desktop notification (`kitten notify`, OSC 99, `o=unfocused`) | taskfw `taskfw/kitty/hook.py:desktop_notify` | `Notification` only, unless `TASKFW_NOTIFY=0` | task tabs only |

They are wired to **different events**, so a finished turn produces exactly one
alert (the chime) even in a task tab. The only near-coincidence is an
`idle_prompt` Notification arriving ~60s after a Stop: the chime already played
for the turn, then a desktop notification arrives for the idle period. That is
a sequence about one idle stretch, not two alerts for one turn end.
