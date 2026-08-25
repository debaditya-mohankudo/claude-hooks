# claude-hooks

This repo gives a coding agent a memory and a context. Hooks fire on every turn,
assemble what's worth knowing — memories, active task, architectural concepts,
related commits — and inject it before the model sees the prompt.

That premise has one hard consequence, and it shapes everything here: **context
the agent trusts but that isn't true is worse than no context at all.** A missing
memory costs a lookup. A confidently wrong one costs a wrong decision, made fast,
with no reason to double-check. Most of the design below is an answer to that.

## What this project believes

**Anything asserted must be checkable by something that runs.**
A rule stated in a doc and enforced by nothing is a comment. It stays plausible
while the code drifts underneath it, and the drift surfaces only when someone
acts on it and gets hurt. Concept drift detection runs on every edit; the gates
run on every tool call; the model tests run on every commit. When you add a claim
about how this system behaves, add the thing that fails when it stops being true.

**Prefer the durable record to the cache.**
Git history is the record; the commit map is a cache over it and is rebuildable
from scratch. The same shape recurs — derive from the source of truth rather than
maintaining a second copy that can silently disagree. When you must cache, make
losing it cheap.

**Fail open, never block the human.**
Every hook in this pipeline sits between the user and their own tools. A gate
that errors, a server that's down, a parse that fails — none may turn into a
blocked call. Degrade, log, and get out of the way. The one exception is a
deliberate deny with a reason the agent can act on, and even that must be
precise: a gate that fires on the wrong thing trains people to route around it,
which costs more than the check was ever worth.

**Say what was removed, and why.**
This codebase is full of tombstones — comments where a function used to be,
naming the task that removed it and the reason. They're kept on purpose. Without
them the next reader cannot tell "deliberately absent" from "nobody got to it
yet", and re-adds what was removed for good reason. Deleting code is easy;
deleting the knowledge of why is the expensive mistake.

**Own one thing.**
Task tracking and the concept tools live in the sibling task-framework project,
not here, and duplicates that grew back were removed rather than maintained in
parallel. Two implementations of one idea will disagree eventually, and the
disagreement will be discovered by whoever trusted the wrong one. When something
here starts to look like a second copy of something there, delete this one.

**Verify the premise, especially your own.**
The most expensive errors in this repo's history came from confident claims
nobody checked — a file that "obviously" existed, a path that was "clearly" the
production one. A premise you wrote an hour ago is exactly as unverified as one
you inherited, and feels more trustworthy, which makes it worse. One concrete
check — read the file, grep the callers, look at the running process — is
usually the whole cost.

## Where truth actually lives

Not in this file. This file goes stale the same way any prose does, so it holds
principles rather than specifics, and points at things that are checked:

- **`concept_store/concepts.json`** — architectural facts per module, with
  invariants and contracts. Drift-checked on every edit. Read and write it
  through the `concept__*` MCP tools.
- **`ontology/claude-hooks-domain.json`** — the domain vocabulary: bounded
  contexts, terms that span modules (SessionState, GateContext, NodeRegistry),
  and typed relations between them. Answers "what is this thing, and how does
  it relate to the others" — concept_store answers "what does this module
  promise" instead. Not drift-checked; re-verify evidence against source
  before trusting a term.
- **`models/*.sysml`** — the structural model and its requirements, enforced by
  `tests/test_models.py`.
- **`tests/`** — the compatibility contract. When behaviour is load-bearing,
  it's pinned there, and the test names say why.
- **The running system** — `launchctl list | grep claude-hooks`, the log tables,
  the live process. Editing this repo's hook code does not change the running
  server until it restarts; that gap has fooled people before.

Read logs through the `hooks__read_logs_sqlite` MCP tool and memories through the
`memory__*` tools rather than opening the databases directly — the tools apply
the filtering and encoding the raw tables don't.

## Working here

Small, verified steps beat large plausible ones. Run the unit suite — it's fast
and needs no server. Put `task:<id>` in every commit message; that reference is
the durable link between code and the reasoning behind it, and everything else
about commit tracking is derived from it.
