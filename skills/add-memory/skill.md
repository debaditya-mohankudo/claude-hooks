---
name: add-memory
description: Save something into MEMORY.sqlite at the right shape -- think first (is it worth saving, new memory or update, type, slug, tags that recall will match, which neighbours to link), then write through `mac memory`. Use when asked to "save this as memory", "remember this", "add a memory", "add tags / relates_to to a memory", or after an analysis, decision or piece of feedback worth keeping. Args: what to save (default: the substance of the conversation so far); optional name:<slug> or type:<user|feedback|project|reference> to skip deciding.
user-invocable: true
updated: 2026-11-22
---

Canonical home: this repo (claude-hooks) — `mac memory` writes the store that `LoadMemoriesNode`
reads, so the skill ships with the code that recalls from it. Deploy: `docs/skills.md`.

# Add memory

## Intent

A saved memory is injected into later sessions and trusted without being re-checked. A wrong one costs more than a missing one: save only what you can support, mark what is unverified, and saving nothing is a valid result.

Mirror of `vault-save`: **think first, search the store, then write.** Do not start with
`mac memory add`. The expensive mistakes are a duplicate of an existing memory, tags that
recall never matches, a re-add that silently wipes `related` or the body, an edge to a loosely
related neighbour, and a stale edge nobody can remove through the CLI.

Memory lives only in `~/.claude/MEMORY.sqlite` (SQLCipher). Use `mac memory` through Bash. Never
write `.md` memory files under `~/.claude/projects/*/memory/` (the global CLAUDE.md overrides the
harness default). `mac memory --help` lists the verbs: `add`, `add_batch`, `get`, `list`,
`search`, `stale`, `validate`, `delete`.

## 1. Think (before any tool call)

Write these down in one block:

- **Worth saving?** Save what is not derivable from the repo, git history or CLAUDE.md: a
  decision and its why, user feedback or a confirmed approach, a dated analysis read with its
  open items, a pointer to an external resource. Do not save code structure, a past fix the
  commit already records, or a daily market/graph read (that goes to the vault via `vault-save`). If asked to save one of those, ask what was non-obvious and save that.
- **What is it?** `feedback` (how to work, with why), `project` (ongoing work, dated read,
  decision), `reference` (where to find something), `user` (who the user is).
- **New or update?** A correction or a fresh read of the same topic updates the existing
  memory. A new date or a new event is a new memory that links to the old one. A memory the new
  one overrides gets a `supersedes` link (the old one is then never injected, so use it only when
  the old one is wrong, not merely older).
- **Content.** The substance the conversation produced: figures exact, sources named, estimates
  and unverified claims marked as such, absolute dates (never "today" or "Friday"). Lead with the
  finding, then `**Why:**` and `**How to apply:**` lines for feedback and project memories. For
  this user (retired, capital preservation) a market memory says so in How to apply when it bears
  on a buy/hold call.
- **Sensitivity.** No credentials, account numbers or positions. Positions live in
  the portfolio file, not memory.

## 2. Search the store (the graph picks the neighbours)

Run in order.

1. `mac memory search "<2-4 keywords>" --detail slim` for the topic, then once more with the
   ticker or event date. A hit on the same topic means update or link, not a new row. Read the
   closest with `mac memory get <slug>` (JSON: `body`, `tags`, `related`, `links`).
2. **Look at the neighbourhood.** `mac memory get` each candidate neighbour and read its `tags`
   and `links`. The convention for slugs and tags comes from the siblings:
   `<topic>-<yyyy-mm-dd>` or `<topic>-<mon>-<yyyy>` for dated reads, lowercase hyphenated.
3. Choose links from the nearest topical siblings: the same event, the previous or next leg of
   the same story, the company or graph memory the new fact should update. Skip a neighbour that
   only shares a domain tag. Two or three good links beat six loose ones.

## 3. Decide, then say it

State the plan in three lines before writing: `Save: <slug> (<type>, new|update)`, `Tags: ...`,
`Links: <slug> (<link type>) ...`. If the update or link choice is a real tie, ask once; otherwise
proceed.

**Slug.** Descriptive, kebab-case, with the date or month for a dated read. No spaces.

**Tags.** Recall is BM25 over `tags` plus the words of the slug, and the body is not scored. So
tags are the whole lever, and a rare keyword outweighs a common one:

- The domain tag of the project the memory is about (`claude-hooks`, `stock-tracker-mcp`, ...);
  copy it from the nearest neighbour's `tags`.
- `skill-<name>` when the memory should be injected as the Skill tool runs `<name>` (exact tag
  match, max 3; see `docs/skills.md`). A re-add keeps stored `skill-*` tags.
- The entities (tickers, companies, indices, instruments), the event (`sell-off`, `SEC probe`),
  the date in ISO form when the memory is dated, and the phrasings the user would type
  (`nvidia` and `NVDA`; `gates` and `PreToolUse`).
- Reuse tags the neighbours already carry (that is what ties a cluster together); check 2-3
  near variants before inventing a new one.
- Tag only what the memory is about, not everything it mentions.

**Links.** `--related a,b` is the plain list; `--links '[{"to":"slug","type":"relates_to"}]'`
adds typed edges. Types: `relates_to`, `supersedes`, `contradicts`, `learned_from`, `extends`.
`relates_to` is stored symmetric (shown as `direction: both`). Add `--files` / `--docs` when the
memory is about specific paths.

## 4. Write

Put the body in a file in the scratchpad (a heredoc inside `--body` trips the allow rule):

```bash
mac memory add <slug> --type project \
  --tags "<domain-tag> entity event 2026-11-20 ..." \
  --body-file <scratchpad>/<slug>.md \
  --related slug-a,slug-b \
  --links '[{"to":"slug-a","type":"relates_to"},{"to":"slug-b","type":"relates_to"}]'
```

Several memories in one pass: one `mac memory add_batch --memories-file FILE.json` (a JSON list of
`{name, type, body, tags, ...}`), not N serial adds.

**Updating an existing memory (tags, links or body).** `mac memory add` is an upsert that replaces
every column it is given and **resets `related` to empty when `--related` is omitted**. So:

1. `mac memory get <slug>` and keep its current `body`, `tags`, `related` and `links`. Build the
   whole update from this JSON, not from an earlier scratchpad body file or from what you
   remember passing before (the stored row is the source of truth and may have been edited since).
   Write `body` to a file, set `--tags` to `tags` plus the new ones, `--related` to `related`,
   and `--links` to `[{"to": l.name, "type": l.type}]` for each entry in `links` plus the new ones.
   Dedupe new tags case-insensitively against the old string. Afterwards `mac memory get` again and confirm
   the body length and `related` match what you started with.
2. Re-add with the full current body (never a placeholder: a placeholder body overwrites the
   real one) and with `--tags` set to the old tags plus the new ones, `--related` and `--links` set
   to the old list plus the new ones.
3. To add tags or edges to *several* memories at once without re-adding bodies, edit the rows
   directly through `encdb` (below). That is a write to the shared store: snapshot first, say so,
   and do it only when the user asked for the edit.

## 5. Verify and report

`mac memory get <slug>` and confirm tags, `related` and `links` came back as intended (for an
update, that nothing was lost). Report in a few lines: the slug, new vs updated, the tags added,
the links added, and anything left out. Do not repeat the body.

## Direct edits through `encdb` (rare, ask first)

Only for what the CLI cannot do: removing an edge, or bulk tag/edge edits. `MEMORY.sqlite` is
encrypted, so plain `sqlite3` fails; open it through the helper after copying a snapshot
(`cp ~/.claude/MEMORY.sqlite ~/.claude/MEMORY.sqlite.pre-<what>.$(date +%Y%m%dT%H%M%S)`):

```bash
cd ~/workspace/claude-hooks && uv run python -c "
import sys; sys.path.insert(0, 'src')
from encdb import open_db
c = open_db('~/.claude/MEMORY.sqlite')          # readonly=True for reads
ids = {r[1]: r[0] for r in c.execute('SELECT id,name FROM memories')}
a, b = sorted((ids['slug-a'], ids['slug-b']))   # symmetric edges need src_id < dst_id
c.execute(\"INSERT OR IGNORE INTO memory_links(src_id,dst_id,type) VALUES (?,?,'relates_to')\", (a, b))
c.commit()"
```

- Edges live in `memory_links(src_id, dst_id, type)`; the `related` column is the legacy copy, so
  keep both in step.
- Removing a slug from `related` does **not** delete its edge. Delete the `memory_links` row for
  the pair, in both directions, and update `related`.
- A symmetric edge written with `src_id > dst_id` is refused (`symmetric link must be stored with
  src_id < dst_id`); the failed transaction is not committed.
- Do not touch `updated` for a tags-only edit; a tags change already makes the BM25 index rebuild.

## Traps

- **Omitting `--related` on a re-add wipes the links column.** Always pass it back.
- **A placeholder body overwrites the real body** (92 memories once lost theirs this way). Fetch
  the body with `mac memory get` first.
- **A re-add replaces ordinary tags; only stored `skill-*` tags survive.** Tested 2026-11-24 on
  a throwaway memory: tags `alpha beta skill-foo`, then a re-add with `gamma` stored
  `gamma, skill-foo`. A tags-only update with just the new tag therefore wipes the old ones
  (it happened on `read-md-notes-with-glow-in-new-kitty-tab`). Always pass the old tags plus the
  new ones. `add` can never remove a `skill-*` tag; removal needs the `encdb` edit below
  (snapshot first). The 2026-11-21 check that said "merges" only tested a `skill-*` tag.
- Any `add` / `add_batch` write, even tags-only, stamps `last_validated` to now. It records that
  the row was written, not that its claims were re-verified.
- A re-add kept `domain` (`claude-hooks`) and `type` as stored (verified on a tags-only update;
  `files` and `docs` were empty there, so pass them back when set). `related` is the column known
  to reset, so always pass it back.
- Do not link by domain alone; the recall graph lifts neighbours of a hit, so a loose edge injects
  noise into unrelated prompts.
- `supersedes` removes the old memory from injection; a stale-but-not-wrong memory wants `extends` or
  `relates_to`.
- The hook server caches the BM25 index and may need a restart before new edges affect recall.
- Everything a web search returned is a claim: mark it unverified in the body, name the sources,
  and note what is open.

## Worked examples

Repo decision: "remember that the gate must fail open" -> Think: `feedback`, the why is in
CLAUDE.md principles but the specific incident is not -> `mac memory search "gate fail open"` finds
`claude-hooks-gate-framework` -> update-or-new is a tie, so a new `gate-fail-open-<incident>` that
`extends` the framework memory, tags `claude-hooks gates PreToolUse fail-open deny`, no link to
memories that only share the `claude-hooks` tag.

Market read:

"save this as memory" after the Nvidia 14% sell-off answer -> Think: `project`, a dated read from
press reports (unconfirmed), not in the repo, bears on a buy call -> `mac memory search "nvidia
SEC"` finds the Nasdaq 11-20 read and the Nvidia company graph, no same-topic memory -> new
`nvda-crash-sec-probe-nov-2026`, tags `stock-tracker-mcp nvidia NVDA SEC probe sell-off ai-capex
...`, links `relates_to` the Nasdaq 11-20 memory, the Nvidia knowledge graph and the factor-graph
snapshot (not the metals sell-off, which only shares the month) -> body file in the scratchpad ->
`mac memory add` with `--related` and `--links` -> `mac memory get` to confirm.
