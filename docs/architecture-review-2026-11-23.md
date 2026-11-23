---
tags: architecture review, claude-hooks, design review, drift, checkpoint leak, MemorySaver, LangGraph, fail-open, ontology, technical debt
---
# Architecture review — 2026-11-23

Scope: whole repo at `660d724`. Started from `CLAUDE.md` and
`ontology/claude-hooks-domain.json`, then checked their claims against the
code, the running server (launchd `com.debaditya.claude-hooks-pipeline`),
`~/.claude/claude_hooks.sqlite` and the cli-graph `mac` consumer. Each finding
names the check behind it. Unit suite at review time: **618 passed, 1 skipped
(59 s)**.

## Verdict

The basic shape is sound: a thin, fail-open client in front of a persistent
local server, ownership extracted cleanly to task-framework, and tombstones
that record why things were removed. The weak point is the middle layer. The
LangGraph pipeline and its checkpointed `SessionState` were built for
cross-hook reasoning that no longer happens. Today the layer costs latency and
leaks memory, while most new behaviour has been added outside it. The second
problem is that the docs which claim to be checked mostly are not. That
directly contradicts the project's first principle.

## What is working

- **Thin client, persistent server.** `hooks/client.py` is about 60 lines,
  stdlib only, with a 2 s timeout, and it fails open. The server owns all heavy
  imports. This is the right split for a hook that runs on every keystroke-turn.

- **Ownership boundary with task-framework.** The commit gates no longer touch
  any task store (`hooks/gates.py` registry comment), and every task-push node
  was removed rather than left half-alive. "Own one thing" is visibly practised.

- **Tombstones work.** During the review it was always possible to tell
  "deliberately absent" from "not done yet" (e.g. `session_graph.py` UPS chain,
  `src/dispatcher.py` DOMAIN_MAP).

- **Test isolation by construction.** `TEST_SESSION_PREFIX` routes integration
  traffic to a separate graph, so tests no longer pollute production state.

- **Measurability.** Every handler logs elapsed time to SQLite, so the latency
  figures below came straight from the logs.

## Findings

Severity reflects user-visible impact or the risk of trusting wrong context.

| # | Sev | Finding | Evidence | Recommendation |
|---|-----|---------|----------|----------------|
| 1 | High | **Checkpoint trim does not free memory.** `_trim_thread_checkpoints` deletes `storage` and `writes` entries but never `checkpointer.blobs`, where the channel values actually live. | Probe: 60 `update_state` calls on one thread, then trim with cap=5 → checkpoints 64→5, blobs **88→88, 1.77 MB→1.77 MB**. | Delete blobs whose `(thread, ns, channel, version)` is not referenced by any kept checkpoint, and add a test that asserts blob bytes drop. Better still, see #2. |
| 2 | High | **Cross-hook history has no consumer.** `session_tools` (full `tool_input` per call, including Write bodies), `session_prompt_texts` and `prompt_tools` grow for the whole session and are copied into a new blob on every PostToolUse. That makes memory O(n²) per session. Their only reader is `GateContext.called_recently/called_this_session/prev_tools`, and neither remaining gate calls those. | `grep` of all three fields; `GitCommitGate`/`GitCommitMcpGate` read only `ctx.tool_input`. Sessions that never send SessionEnd (crash, kill) leak until the server restarts. | Stop recording `tool_input` in `session_tools`, or drop the history fields and helpers (with a tombstone) until a gate needs them. |
| 3 | Medium | **The graph is no longer where behaviour lives.** The 4 chains are 1–3 trivial nodes each. PostToolUse runs about 8 side-effects in `dispatcher._handle_post_tool_use` *before* the graph: WebSearch cache, find_cli log, prompt shadow, vault reindex ×2, context nudge, skill memories, tag nudge. The client runs 4 more: kitty, dashboard plan/turn, last_response. ARCHITECTURE's "adding behavior means adding a node" is no longer the practice. | `hooks/dispatcher.py:274-396`, `hooks/client.py:40-47`. PreToolUse costs **43 ms median** for what is a regex, because each event pays for `get_state`×2 + `invoke` + `update_state`. | Choose one orchestrator. Recommended: make the dispatcher the explicit orchestrator and shrink per-session state to a small dict (`prompt_id`, `turn`, `stop_alert_sent`, `sound_played`). Keep LangGraph only if `langchain_learning` is a deliberate learning goal, and say so in ARCHITECTURE. |
| 4 | Medium | **The fail-closed path does not run in production.** `_FAIL_CLOSED_COMMANDS` (`mac imessage send`, `mac mail compose`) is applied only in `dispatcher.main()`, the subprocess path. In production a handler exception becomes a 500 in `server.py`, the client catches it and prints `{}`, and the call is **allowed**. The gates those commands needed already moved to LocalMacMCP. | `hooks/dispatcher.py:428,640-654` vs `hooks/server.py:67-70` and `hooks/client.py:61-63`. | Remove it with a tombstone (LocalMacMCP owns those sends), or implement it in the server route. Don't keep code that implies a protection nothing provides. |
| 5 | Medium | **UserPromptSubmit runs close to the client timeout, and handlers block the event loop.** Over 386 UPS requests: median 715 ms, p95 1144 ms, max 2151 ms (graph-internal max 2929 ms). Above 2 s the client fails open and that turn **silently loses all injected context**. All routes are `async def` but call synchronous handlers, so one slow UPS stalls every other session's PreToolUse. | `hook_logs` `HTTP POST /hook/*` rows. | Profile `load_memories` (most of the 650 ms is graph time, not the 28 ms prompt shadow). Make routes `def` (threadpool) with a per-session lock. Log client-side timeouts to a file the dashboard can show. |
| 6 | Medium | **The docs claim checks that don't exist (principle #1 violated by the doc that states it).** | See the drift list below. | Fix the text, and add an evidence test for the ontology (sketch below). |
| 7 | Medium | **Cross-repo break: `mac diff_rag` is broken.** cli-graph still wraps `diff_rag__*`, which `c239f888` removed here. | `mac diff_rag query … --json` → `ModuleNotFoundError: No module named 'tools.diff_rag'`. | Remove the group in cli-graph (task there). The `_hooks.py` subprocess runner is the real consumer of `src/tools/*.handle_*`, so a contract test listing the handlers cli-graph expects would have caught this. |
| 8 | Low | **The tmux nudge matches substrings.** `"serve"` matches `mcp_server`, `server.py`, `observe…`. | Fired during this review on a `curl …/health` command. | Match tokens or command heads, not substrings. |
| 9 | Low | **The memory header renders `### name []`.** `_format_system_prompt` still prints `[{domain}]` after the domain column was dropped (`scripts/drop_memory_domain_column.py`). | Visible in every injected memory this session. | Drop the bracket. |
| 10 | Low | **Unused runtime dependencies.** No non-test import of `langchain-anthropic`, `langchain-community`, `langchain-classic`, `anthropic`, `numpy`, `msgpack`, `jinja2`, `markdown`, `httpx2`, `python-multipart`. `pytest-xdist` is in runtime deps. | PCRE `git grep` per module. | Prune them (it speeds up server start and shrinks `uv.lock`), and move xdist to dev. |
| 11 | Low | **Dead state fields.** `SessionState.active_task_domain`, `task_files` and `backfill_count` are fed only by the removed `ActivateTaskNode`. | `session_state.py` comments; not in `_fresh_state`. | Remove them, or tombstone them alongside `BackfillNodeProtocol`. |
| 12 | Low | **The commit gate checks the shape of the id, not that it exists.** Any `task:[a-f0-9]{6,}` passes. That is a reasonable fail-open choice with no taskfw dependency, but the README says it is "rather than trusting Claude's own claim that one exists" and that "it needs database access to resolve active tasks". Both are false. | `hooks/gates.py:160`, README "Gates". | Make the README say what the gate actually checks. |
| 13 | Low | **Repo-root litter.** `file:testlogs?mode=memory&cache=shared` (an SQLite URI opened without `uri=True` in some test), the stale `.code/.diff/.fullrepo/.tasks_embeddings.*` from the removed RAG stack, and `.claude/worktrees/task-document-experiment`. | `ls -la`. | Find the test opening the URI, and delete the leftover artifacts. |

### MCP server

`mcp_server.py` + `src/dispatcher.py` are **not registered** anywhere. Only
`taskfw` and `find_cli` are configured. The owner confirmed this is intentional:
the server is kept for possible future use. The handlers stay live because
cli-graph's `mac memory`/`mac hooks` call them through a subprocess. What is
needed is to label it, not remove it:

- `mcp_server.py` docstring still says "task, memory, session, hooks tools".

- `CLAUDE.md` tells agents to use `hooks__read_logs_sqlite` and `memory__*`
  MCP tools, which don't exist in any session. It should say `mac hooks …` /
  `mac memory …`.

- ARCHITECTURE lists it as the "MCPTools" subsystem with no "dormant" note.
  Add one line, and keep `tests/test_src_dispatcher.py` so it doesn't rot while
  dormant.

## Documentation and ontology drift

**`CLAUDE.md` (repo)**

- "Concept drift detection runs on every edit". No hook does this. The
  drift-detection hook (`diff_hook.py`) was retired in `task:85e6001f`
  (`src/dispatcher.py` tombstone), and `concepts.json` is not drift-checked by
  any test here.

- "Read logs through the `hooks__read_logs_sqlite` MCP tool…": not registered
  (see above).

**`docs/ARCHITECTURE.md`**

- "`client.py` falls back to `dispatcher.py` in subprocess mode". It doesn't;
  it prints `{}`.

**`hooks/server.py` route docstrings**

- UPS: "tool hints, domain classification, and task context" (all removed).

- PreToolUse: the `imessage__send`/`contacts__search` example (moved to
  LocalMacMCP).

- PostToolUse: "task-lifecycle bridge nodes" (removed).

- `/session/memory`: "assistant turns" (the global CLAUDE.md says none are
  recorded).

**Global `~/.claude/CLAUDE.md`**

- Says tool hints are injected under `## Suggested tools`. That block was
  removed (`dispatcher._format_system_prompt` tombstone).

**`ontology/claude-hooks-domain.json`** (checked by a script that resolves
every `evidence` pointer):

- `ToolScorer` → `retrievers.py:ToolScorer`: **symbol no longer exists**
  (score_tools removed). Its relations to `MemoryRetriever` and
  `ToolDomainMap` go with it.

- `PreToolUse`, `PostToolUse` → `dispatcher.py:669`: a line number (which the
  file's own rules forbid), and **past EOF** (the file has 661 lines).

- `SourceModule` is used in a relation but never defined. `NoopNode` has no
  relations.

- The `UserPromptSubmit` definition still mentions tool hints. The "Memory &
  Retrieval" context says "keyword-overlap"; it is BM25 plus the memory_links
  graph.

- "MCP Tool Surface" describes a server nobody connects to. The live surface
  is `src/tools/*.handle_*` reached through cli-graph's `_hooks.py` runner.
  Recommendation: rename it to "Tool Handler Surface" with two front-ends,
  `mac` (live) and `mcp_server.py` (dormant).

- **Missing terms** for behaviour that now carries real weight: PromptShadow,
  FindCliLog, WebSearchCache (a PreToolUse *deny* used as a cache hit),
  VaultIndex reindex, SkillMemories / tag nudge, ContextSizeNudge, client-side
  TerminalStatus (kitty, dashboard plan/turn, last_response), CacheStore,
  EncDB.

**Proposed check** (fits "anything asserted must be checkable"): a
`tests/test_ontology.py` that, for every term, asserts the evidence file exists,
the symbol is defined in it, and no pointer is a bare line number. It should
also assert that every relation endpoint is a defined term. The script used for
this review does exactly that in about 15 lines.

## Suggested order

1. #1 + #2 together (leak; mostly deletion) → a test asserting blob bytes stay bounded.
2. #4 (remove the inert fail-closed path, or make it real).
3. #6 docs + the ontology test; mark the MCP server dormant.
4. #7 in cli-graph.
5. #5 latency profile, then the #3 orchestration decision, which is easier once #2 has shrunk the state.
6. Low items opportunistically.
