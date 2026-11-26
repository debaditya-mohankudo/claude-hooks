---
tags: claude-hooks, skills, skill usage, commit skill, add-memory, vault-save, skill reminder, slash command memories, memory injection
---
# Skill usage review: skills run without being loaded (2026-11-26)

The review itself changed no code; item 1 was built later the same day (task:df16e538). Window: 14 days of main-session transcripts.
Figures come from pattern matching on those transcripts and were not re-run when
this file was written. "Earlier review" is
[daily-workflow-review-2026-11-26.md](daily-workflow-review-2026-11-26.md).

## Verdict

The skills are fine. The gap is that they often aren't loaded when their work
happens, and the hooks don't notice.

| Action | Ran without its skill | Measure | With skill loaded | Without |
|---|---|---|---|---|
| `git commit` | 125 of 198 (63%) | test run in the same turn | 86% | 22% |
| `mac memory add` | 74 of 124 (60%) | has `--links` | 92% | 47% |
| | | searched first, same turn | 96% | 58% |

This is a correlation. The commit gap is probably inflated, since docs-only
commits and repos without tests are likelier to skip the skill. The memory gap
is the cleaner evidence.

## What to change, in order

| # | Change | Why | Size |
|---|---|---|---|
| 1 | Remind when a skill's action runs without the skill. One advisory note per session, triggered by the command: `git add` or `git commit` → `/commit`, `mac memory add` → `/add-memory`, `mac vault` writes → `/vault-save` | The table above | Small; the command parsing and once-per-session pattern already exist |
| 2 | Stop injecting memories where they can't help: agent and notification messages, `/lastresponse`, `/handoff`, very short follow-ups, and names already injected this session | Findings 1 and 2 of the earlier review; `load_memories.py` does neither | Small |
| 3 | Give typed slash commands their skill memories. The lookup runs only on a `Skill` tool call (`hooks/dispatcher.py:361`), so typing `/name` skips it | Market graph skills: 142 typed, 15 through the tool | Small |

**On 1:** a note added before a tool call reaches the model only with the
result, so the first command still runs. Triggering commit on `git add` gets
there in time when staging is a separate call. For memory and vault writes, it
can only prompt a fix afterwards. No blocking: 31 vault writes outside the
market skills skipped `/vault-save`, and some of those may be legitimate.

**On 3:** the payoff is small today. Only a few memories are really tagged for a
skill (`vault-save`, `vault-search`), none for the market graph skills.

## Not to add

- **A prompt-time skill router.** Skill descriptions already do that matching,
  and there is no evidence they pick the wrong skill. The similar `find_cli`
  guess at prompt time matched only 7% and was removed.
- **More always-on context.** Each prompt already gets about 1,100–1,600 tokens
  (last 3 logged).

## Next

Build 1 first, then rerun the same measurement after a week to see whether the
two "without" figures move.

## Baseline for the rerun

`uv run python scripts/measure_skill_use.py --until 2026-11-26T07:21:17Z` (the
time of commit 4b80513, when the reminder went live; 14-day window):

| Action | Without its skill | Measure | Loaded | Without |
|---|---|---|---|---|
| `git commit` | 127 of 201 (63%) | test run in the same turn | 64 of 74 (86%) | 30 of 127 (24%) |
| `mac memory add` | 82 of 132 (62%) | has `--links` | 46 of 50 (92%) | 35 of 82 (43%) |
| | | searched earlier in the turn | 45 of 50 (90%) | 37 of 82 (45%) |
| `mac vault` writes | 38 of 118 (32%; 213 more under a market graph skill) | | 80 | |

This replaces the first baseline, which counted a skill loaded later in the same
turn as loaded and picked transcripts by file modification time (task:1b86af46).
Ordering moved one commit and no memory add from loaded to bypassed. "Searched
first" fell most (it also counted a search made after the add).

The patterns match command text, not parsed calls. In this window, 25 of the 132
`mac memory add` matches and 6 of the 201 commit matches are not matches for the
reminder's own parser, so some are mentions inside another command.

For the rerun, on or after 2026-12-03:
`uv run python scripts/measure_skill_use.py --since 2026-11-26T07:21:17Z`.
Compare shares, not counts, since the window is shorter.

Expect the commit figure to move little: of 296 commit calls, 265 had `git add`
in the same Bash call, so the note usually arrives after the commit.
