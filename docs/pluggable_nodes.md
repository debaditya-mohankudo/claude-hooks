# Pluggable Nodes

The UPS graph exposes extension points where default nodes can be swapped without
touching core activation logic. The graph is the registration mechanism — no
subclassing, no config flags.

## Backfill slot

After `ActivateTaskNode` runs, a conditional edge fires when `state["task_files"]`
is non-empty. The default implementation is `BackfillMemoryFilesNode` (token-overlap
strategy). To replace it, build your own graph and wire a different node into the
same slot.

### Contract (`BackfillNodeProtocol`)

Any callable satisfying this shape can occupy the slot:

```python
(state: SessionState) -> dict
```

**Reads from state:**

- `task_files: list[str]` — file paths from the active task's `Files:` section
- `active_task_domain: str` — domain tag of the active task (e.g. `"claude-hooks"`)
- `session_id: str` — replay guard: skip writes when `session_id` starts with `replay-`

**Returns (partial state update):**

- `backfill_count: int` — number of memory records written; `0` if skipped

### Functions are first-class (but prefer the class convention)

A plain function satisfies the protocol — no class required. Use it for test
stubs, one-liners, or throwaway experiments. For production nodes, prefer the
class form (see "Convention vs. function" below) — the cost of breaking
convention is small but real.

```python
def my_backfill(state: SessionState) -> dict:
    files  = state.get("task_files") or []
    domain = state.get("active_task_domain") or ""
    # ... your strategy ...
    return {"backfill_count": n}
```

Wire it directly:

```python
from langchain_learning.nodes.activate_task import ActivateTaskNode
from langchain_learning.session_graph import build_graph

builder.add_node("activate_task",        ActivateTaskNode())
builder.add_node("backfill_memory_files", my_backfill)   # function, not class
builder.add_conditional_edges(
    "activate_task",
    lambda s: "backfill_memory_files" if s.get("task_files") else END,
    {"backfill_memory_files": "backfill_memory_files", END: END},
)
builder.add_edge("backfill_memory_files", END)
```

### Convention vs. function — when the class form matters

The class-with-`__call__` convention exists for one concrete reason: the first line
of every default node calls `entry("<node_name>", state)`, which writes a structured
log event to `claude_hooks.sqlite`. A plain function skips this unless you add it
explicitly.

**If you care about observability** (the hook server's event log, `/what-am-i-working-on`,
task history injection), add `entry()` as the first call:

```python
from langchain_learning.nodes._node_log import entry

def my_backfill(state: SessionState) -> dict:
    entry("backfill_memory_files", state)   # keeps you in the event log
    ...
```

**If you don't need the log event** (e.g. a test stub, a silent no-op), a bare
function is fine and costs nothing.

There is no decorator pattern or `isinstance` check in the graph — both forms are
fully interchangeable for routing, patching, and composition.

### Slot policy

One backfill slot only. Multiple strategies must be composed *inside* one node —
do not add parallel edges after `activate_task`. This keeps the graph topology flat
and the extension point unambiguous.
