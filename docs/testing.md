---
tags: testing strategy, pytest, unit tests, API tests, integration tests, replay harness, test suite, mock, fixtures, test_langchain_session_graph, test_replay_harness, uv run pytest, SqliteSaver, MemorySaver, test isolation
---
# Testing Strategy

Four complementary test layers, each catching different classes of bugs.

```
Unit tests        → behavioral correctness of nodes and gates
API tests         → HTTP wire layer, route dispatch, response shape
Integration tests → live server contracts and end-to-end flows
Replay harness    → graph stability against real production traffic
```

Run unit + API tests (default CI):

```bash
uv run python -m pytest tests/ -v
```

Run everything including integration tests (requires server on :8766):

```bash
uv run python -m pytest tests/ -v -m "integration or not integration"
```

---

## 1. Unit Tests

**What they cover:** Individual node logic, gate decisions, memory loading, tool scoring, session state transitions.

**File pattern:** `tests/test_*.py` (excluding `test_server_api.py` and `test_replay_harness.py`)

**Key files:**
- `tests/test_gates.py` — gate allow/deny logic for all gated tools
- `tests/test_session_graph.py` — LangGraph node execution and routing
- `tests/test_logger.py` — prod schema contract and `emit()` behavior
- `tests/test_session_tools.py` — MCP task/memory tool handlers

**How they work:**

Unit tests call nodes and graph functions directly, bypassing HTTP. Log output is captured in a named shared in-memory SQLite DB (`file:testlogs?mode=memory&cache=shared`) during the run and dumped to `tests/test_logs.db` at session end. The `_log_test_marker` fixture scopes log queries to the current test.

**Log inspection:**

```bash
# After a run, query test_logs.db directly
sqlite3 tests/test_logs.db "SELECT logger, message FROM hook_logs ORDER BY id DESC LIMIT 20"

# Or use the skill
/analyze-test-logs
```

**Invariants enforced:**
- Every ALLOW-path `imessage__send` test must have `found_in_recent=True` in gate logs
- Every DENY-path test must produce exactly 1 DENY log row
- `hook_logs` schema must never contain `run_id` column in prod path

---

## 2. API Tests

**What they cover:** FastAPI route dispatch, JSON parsing, lifespan (graph build), response shape, end-to-end HTTP → dispatcher → LangGraph stack.

**File:** `tests/test_server_api.py`

**How they work:**

Uses FastAPI `TestClient` (Starlette) — runs the full ASGI app in-process with no port binding. Lifespan fires so the MemorySaver graph is built once per module. All 4 hook routes plus `/health` and `/session` are exercised.

```python
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from hooks.server import app
    with TestClient(app) as c:
        yield c
```

**Session ID contract:**

All test session IDs must start with `api-test-`. This prevents `LogToolUsageNode` from upserting into the production `tool_hints.sqlite` during test runs. Do not use bare session names like `test` or `sess-1` in this file.

**Routes covered:**

| Route | Class | Key assertions |
|-------|-------|----------------|
| `GET /health` | `TestHealth` | 200, `status=ok`, `sessions` is int |
| `GET /session` | `TestSession` | 200, `count` matches list length |
| `POST /hook/UserPromptSubmit` | `TestUserPromptSubmit` | 200, valid prompt returns `hookSpecificOutput`, empty prompt returns `{}` |
| `POST /hook/PreToolUse` | `TestPreToolUse` | ungated tool → `{}`, gated without prereq → `permissionDecision=deny` |
| `POST /hook/PostToolUse` | `TestPostToolUse` | always returns `{}` |
| `POST /hook/Stop` | `TestStop` | always returns `{}` |

**Run API tests only:**

```bash
uv run python -m pytest tests/test_server_api.py -v
```

---

## 3. Integration Tests

**What they cover:** End-to-end contracts against the live hook server on `:8766`. Tests the full request path including LangGraph graph execution, SqliteSaver checkpoints, and task state transitions.

**File:** `tests/test_review_lifecycle_integration.py`

**Requirements:** Server must be running (`~/workspace/claude-hooks` main branch, port 8766). Tests are skipped automatically when the server is unreachable.

**Key scenarios:**

- `TaskDoneGate` — done blocked/allowed based on task state and review runs
- Manual approval bypass (non-empty reason required)
- Review-tag guard: `review:<template>` tags only valid in review state
- Auto-review transition: UPS `"task:<id> done"` signal moves task to review state

**Isolation:** Each test uses a unique session ID suffix so each `PreToolUse` call gets a fresh LangGraph thread (avoids stale SqliteSaver checkpoints). All test tasks are tagged `test:integration` and cleaned up after each test.

```bash
uv run python -m pytest tests/test_review_lifecycle_integration.py -v
```

---

## 4. Replay Harness

**What it covers:** Graph output stability against real production UPS events. Detects regressions in memory injection, tool scoring, domain detection, and related-task resolution when the graph changes.

**Files:** `tests/replay_harness.py`, `tests/test_replay_harness.py`, `tests/replay_baseline.json`

**Source:** Reads `UPS enter` + `UPS done` pairs from `claude_hooks.sqlite` (`~/.claude/claude_hooks.sqlite`, resolved via `config.log_db`). One event per unique session (last UPS of each session).

**How it works:**

1. **Capture** — load prod UPS events, replay through current graph, save output as `replay_baseline.json`
2. **Replay** — load same prod events, replay again, diff against baseline on `domains`, `memories_count`, `tools_count`, `related`, `rag_chunks`
3. Any deviation = regression

```bash
# Capture a fresh baseline (do this before making graph changes)
uv run python tests/replay_harness.py --capture --since 2026-06-13 --limit 50

# Replay and diff vs saved baseline
uv run python tests/replay_harness.py --replay --since 2026-06-13 --limit 50

# Both in one shot
uv run python tests/replay_harness.py --capture --replay --since 2026-06-13 --limit 50
```

**As a pytest check:**

```bash
uv run python -m pytest tests/test_replay_harness.py -v
```

Skips automatically if `~/.claude/claude_hooks.sqlite` is not present or if no baseline exists. The `test_diff_runs_no_regressions` test compares the last two pytest run_ids in `test_logs.db`.

**When to run:** Before and after any change to LangGraph nodes, memory retrieval, tool scoring, or domain detection. Not part of routine CI — run manually as a smoke test.

---

## Execution Order

Tests run in this order within a single `pytest` session (enforced by `pytest_collection_modifyitems` in `conftest.py`):

```
api tests → unit tests → integration tests → harness
```

API tests run first so a broken server is caught immediately. Integration tests follow unit tests. The harness runs last because `test_diff_runs_no_regressions` depends on the current run's logs being written to `test_logs.db`, which happens at session teardown after unit tests complete.

---

## Log DB

All test logs write to `tests/test_logs.db` (SQLite, gitignored). Accumulated in memory during the run, flushed to file at session end. Each run gets a unique `run_id` (timestamp) so history is preserved across runs.

Schema:

```sql
hook_logs  (id, logger, level, message, ts, run_id)
test_runs  (run_id, ts, n_tests, n_passed, n_failed)
```

Use `/analyze-test-logs` to query and interpret the log DB after a run.
