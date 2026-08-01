"""Structural contracts for all session graph nodes."""
from __future__ import annotations

from typing import Protocol

from langchain_learning.session_state import SessionState


class NodeCallable(Protocol):
    def __call__(self, state: SessionState) -> dict:
        """Execute node logic and return partial state updates."""
        ...


class BackfillNodeProtocol(Protocol):
    """Contract for a pluggable memory backfill node in the UPS graph.

    The graph wires one backfill slot after ActivateTaskNode via a conditional
    edge that fires when state["task_files"] is non-empty. Any callable
    satisfying this protocol can occupy that slot — no subclassing required.

    Inputs (read from state):
        task_files (list[str]):     file paths from the active task's Files: section
        active_task_domain (str):   domain tag of the active task (e.g. "claude-hooks")
        session_id (str):           current session id (replay guard: skip writes for replay-*)

    Outputs (returned as partial state update):
        backfill_count (int):       number of memory records written; 0 if skipped

    To swap the default implementation, build your own graph that imports
    ActivateTaskNode and wires your node in place of BackfillMemoryFilesNode.
    Multiple strategies should be composed inside one node — do not add
    parallel backfill edges.
    """

    def __call__(self, state: SessionState) -> dict:
        ...
