"""Send-gate policy — lookup-before-send enforcement.

Single source of truth for which tools are gated and what prerequisites they
require. Completely independent of DB state — operates purely on GateContext (in-memory dataclass).

Adding a gate for an external MCP tool: edit ~/.claude/gate_rules.yaml — no Python change needed.
Adding a gate with custom DB logic: add a Gate subclass below + register in GATES.

Anti-hallucination principle: Claude cannot be trusted to remember whether it
already verified something. Only tool call records in prompt_tool_calls (written
by the hook infrastructure, not the model) are facts. Gates enforce this.

External tool gate rules live in ~/.claude/gate_rules.yaml (or CLAUDE_GATE_RULES env var).
They are loaded at module import time and registered into GATES automatically.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.logger import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# GateContext — prepared once from SessionState, passed to every gate
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    tool: str
    prompt_id: str
    tool_input: dict = field(default_factory=dict)
    tool_result: dict = field(default_factory=dict)
    found: bool = False
    ts: float = 0.0


@dataclass
class GateContext:
    """Prepared view of session state passed to every gate's verify().

    Built once in gate_check.py from SessionState; each gate uses what it needs.
    """
    tool_name: str
    tool_input: dict

    # Rich call records from prompt_tools (current prompt only)
    current_calls: list[ToolCall]

    # Tool names only from session history (all prompts, keyed by prompt_id)
    session_tools: OrderedDict[str, list[str]]

    # Ordered prompt ids this session
    session_prompt_ids: list[str]

    # Current prompt id
    prompt_id: str

    # Raw prompt text for name presence checks (lower-cased)
    prompt_text: str = ""

    # Current + previous prompt texts (current first); used for multi-turn name checks
    recent_prompt_texts: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.recent_prompt_texts is None:
            self.recent_prompt_texts = [self.prompt_text] if self.prompt_text else []

    def prompt_texts(self):
        """Yield recent prompt texts, current first."""
        yield from self.recent_prompt_texts

    def called_this_session(self, tool: str) -> bool:
        return any(
            (entry.get("tool") if isinstance(entry, dict) else entry if isinstance(entry, str) else None) == tool
            for bucket in self.session_tools.values()
            for entry in bucket
        )

    def called_recently(self, tool: str, window_s: float = 120.0) -> bool:
        """Return True if tool was called within window_s seconds."""
        import time
        cutoff = time.time() - window_s
        for tc in self.prev_tools():
            if tc.tool == tool and tc.ts >= cutoff:
                return True
        return False

    def prev_tools(self):
        """Yield ToolCall objects in reverse call order (most recent first)."""
        history: list[ToolCall] = []
        for bucket in self.session_tools.values():
            for entry in bucket:
                if isinstance(entry, dict) and "tool" in entry:
                    history.append(ToolCall(
                        tool=entry["tool"],
                        prompt_id="",
                        tool_input=entry.get("tool_input", {}),
                        ts=entry.get("ts", 0.0),
                    ))
                elif isinstance(entry, str):
                    history.append(ToolCall(tool=entry, prompt_id=""))
        history.extend(self.current_calls)
        yield from reversed(history)



# ---------------------------------------------------------------------------
# Base Gate ABC
# ---------------------------------------------------------------------------

class Gate(ABC):
    """Abstract base for all gate types.

    Each subclass encapsulates its own verification logic — prereq checks,
    input validation, state checks — and owns its deny message.

    Subclasses implement verify(ctx) -> tuple[bool, str]:
        (True, reason)  → deny the tool call
        (False, "")     → allow

    Logging is handled automatically: the base class wraps verify() at
    instantiation time so subclasses never need to import or call _log.
    """

    tool_name: str

    @abstractmethod
    def verify(self, ctx: GateContext) -> tuple[bool, str]:
        """Return (deny, reason). deny=True blocks the tool call."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _original = cls.__dict__.get("verify")
        if _original is None:
            return

        def _logged_verify(self: Gate, ctx: GateContext) -> tuple[bool, str]:
            tag = f"[{self.tool_name}] prompt={ctx.prompt_id[:8] if ctx.prompt_id else '?'}"
            deny, reason = _original(self, ctx)
            if deny:
                _log.warning("%s DENY reason=%s", tag, reason.split(".")[0])
            else:
                _log.info("%s ALLOW", tag)
            return deny, reason

        cls.verify = _logged_verify


# ---------------------------------------------------------------------------
# Concrete gate classes
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_S = 120.0  # seconds — default staleness window for all prereq checks

# Type alias for a pure verifier function: (GateContext) -> (deny, reason)
# deny=True blocks the tool; deny=False allows it.
# Verifiers are pure — no logging, no side effects.
Verifier = Callable[[GateContext], "tuple[bool, str]"]


# ---------------------------------------------------------------------------
# Verifier factories — one per YAML gate field
# ---------------------------------------------------------------------------

def _make_input_arg_check(gated: str, input_arg: str) -> Verifier:
    """Gated tool's own input[input_arg] must appear as a substring in the prompt."""
    def _check(ctx: GateContext) -> tuple[bool, str]:
        value = (ctx.tool_input.get(input_arg) or "").lower().strip()
        if not value:
            return False, ""  # no value to check — pass through
        found = any(value in pt.lower() for pt in ctx.prompt_texts() if pt)
        _log.info("[%s] input_arg_check %s=%r found_in_recent=%s", gated, input_arg, value, found)
        if not found:
            return True, (
                f"Blocked: {gated} — '{ctx.tool_input.get(input_arg)}' "
                f"does not appear in the current or previous prompt. "
                f"Confirm the intended value first."
            )
        return False, ""
    return _check


def _make_prereq_check(gated: str, prereq_tool: str, window_s: float, name_arg: str) -> Verifier:
    """Prereq tool must have run recently. If name_arg set, its value must appear in the prompt."""
    def _check(ctx: GateContext) -> tuple[bool, str]:
        import time
        cutoff = time.time() - window_s
        for tc in ctx.prev_tools():
            if tc.tool != prereq_tool:
                continue
            if name_arg and not tc.tool_input.get(name_arg):
                continue
            if tc.ts < cutoff:
                continue
            if name_arg:
                searched = tc.tool_input.get(name_arg, "").lower()
                found = any(searched in pt.lower() for pt in ctx.prompt_texts() if pt)
                _log.info("[%s] name_arg_check name=%r found_in_recent=%s", gated, searched, found)
                if searched and not found:
                    return True, (
                        f"Blocked: {gated} — {prereq_tool} was called for "
                        f"'{tc.tool_input.get(name_arg)}' but that name does not appear "
                        f"in the current or previous prompt. Search for the intended recipient first."
                    )
            return False, ""
        qualifier = f" with a non-empty '{name_arg}' arg" if name_arg else ""
        return True, (
            f"Blocked: {gated} requires {prereq_tool}{qualifier} within the last "
            f"{int(window_s)}s. Call {prereq_tool} first, then retry."
        )
    return _check


# ---------------------------------------------------------------------------
# Chain builder — composes verifiers sequentially, short-circuits on first deny
# ---------------------------------------------------------------------------

def _build_gate_chain(rule: dict) -> Verifier:
    """Build a verifier chain from a gate_rules.yaml entry.

    Each YAML field maps to one verifier. The chain runs them in order and
    returns on the first deny. Adding a new gate type = one new factory +
    one new entry here.
    """
    gated = (rule.get("tool") or "").strip()
    verifiers: list[Verifier] = []

    if rule.get("input_arg"):
        verifiers.append(_make_input_arg_check(gated, rule["input_arg"].strip()))

    if rule.get("prereq"):
        verifiers.append(_make_prereq_check(
            gated,
            rule["prereq"].strip(),
            float(rule.get("window_s", DEFAULT_WINDOW_S)),
            (rule.get("name_arg") or "").strip(),
        ))

    def _chain(ctx: GateContext) -> tuple[bool, str]:
        for v in verifiers:
            deny, reason = v(ctx)
            if deny:
                return deny, reason
        return False, ""

    return _chain


def _logged_chain(tool_name: str, chain: Verifier) -> Verifier:
    """Wrap a verifier chain with DENY/ALLOW logging."""
    def _run(ctx: GateContext) -> tuple[bool, str]:
        deny, reason = chain(ctx)
        tag = f"[{tool_name}] prompt={ctx.prompt_id[:8] if ctx.prompt_id else '?'}"
        if deny:
            _log.warning("%s DENY reason=%s", tag, reason.split(".")[0])
        else:
            _log.info("%s ALLOW", tag)
        return deny, reason
    return _run


# ---------------------------------------------------------------------------
# External gate loader — reads ~/.claude/gate_rules.yaml (or CLAUDE_GATE_RULES)
# ---------------------------------------------------------------------------

_GATE_RULES_DEFAULT = Path.home() / ".claude" / "gate_rules.yaml"


def _load_external_gates(path: Path | None = None) -> dict[str, Gate]:
    """Load prereq-style gates from a YAML config file.

    Returns a dict of {tool_name: Gate} ready to merge into GATES.
    Fails open on any error — a missing or malformed config never blocks tools.
    """
    rules_path = path or Path(os.environ.get("CLAUDE_GATE_RULES", str(_GATE_RULES_DEFAULT)))
    if not rules_path.exists():
        _log.debug("[gates] gate_rules not found at %s — skipping external gates", rules_path)
        return {}

    try:
        import yaml  # pyyaml — available in project deps
        with rules_path.open() as f:
            config = yaml.safe_load(f) or {}
    except Exception as exc:
        _log.warning("[gates] failed to load %s: %s — no external gates registered", rules_path, exc)
        return {}

    loaded: dict[str, Gate] = {}
    for entry in config.get("gates", []):
        tool_name = (entry.get("tool") or "").strip()
        if not tool_name:
            _log.warning("[gates] skipping malformed entry (missing tool): %s", entry)
            continue

        prereq_tool = (entry.get("prereq") or "").strip()
        chain = _logged_chain(tool_name, _build_gate_chain(entry))
        cls = type(f"_ExternalGate_{tool_name}", (Gate,), {
            "tool_name": tool_name,
            "verify": lambda _self, ctx, _c=chain: _c(ctx),
        })
        cls.__abstractmethods__ = cls.__abstractmethods__ - {"verify"}  # type: ignore[attr-defined]
        loaded[tool_name] = cls()
        window_s = int(float(entry.get("window_s", DEFAULT_WINDOW_S)))
        _log.info("[gates] registered external gate: %s → prereq=%s window=%ss", tool_name, prereq_tool, window_s)

    return loaded


import re as _re

_GIT_COMMIT_RE = _re.compile(
    r'git\s+(?:(?!commit\b)\S+\s+)*commit\b|git_local\.sh',
    _re.IGNORECASE,
)
_TASK_ID_RE = _re.compile(r'task:[a-f0-9]{6,}')


class GitCommitGate(Gate):
    """Gate for Bash tool calls that contain a git commit.

    Passes through all non-commit bash calls immediately. For commit calls,
    denies if no task:<id> pattern is found anywhere in the command string.
    This enforces traceability — every commit must reference an active task.
    """
    tool_name = "Bash"

    def verify(self, ctx: GateContext) -> tuple[bool, str]:
        command: str = ctx.tool_input.get("command", "")
        if not _GIT_COMMIT_RE.search(command):
            _log.debug("[Bash] non-commit bash — allow")
            return False, ""
        if _TASK_ID_RE.search(command):
            _log.info("[Bash] git commit with task:<id> — allow")
            return False, ""
        return (
            True,
            "Blocked: git commit is missing a task:<id> reference. "
            "Add 'task:<id>' to the commit message body, or activate a task first with tasks__set_active.",
        )


class GitCommitMcpGate(Gate):
    """Gate for git__commit MCP tool — requires non-empty task_id param.

    Cleaner than the Bash regex gate: task_id is a typed param so it
    can never be silently omitted or mangled by shell quoting.
    """
    tool_name = "git__commit"

    def verify(self, ctx: GateContext) -> tuple[bool, str]:
        task_id = (ctx.tool_input.get("task_id") or "").strip()
        if not task_id:
            return (
                True,
                "Blocked: git__commit requires a non-empty task_id for traceability. "
                "Pass the active task ID or activate a task first with tasks__set_active.",
            )
        _log.info("[git__commit] task_id=%s — allow", task_id)
        return False, ""


# ---------------------------------------------------------------------------
# Jira hierarchy gate
# ---------------------------------------------------------------------------

_JIRA_RULES: dict[str, set[str]] = {
    "story":    {"epic"},
    "task":     {"epic"},
    "bug":      {"epic"},
    "subtask":  {"story", "task", "bug"},
    "feedback": {"epic", "story", "task", "bug", "subtask"},
}


def validate_jira_hierarchy(issue_type: str, parent_id: str) -> str | None:
    """Pure validation of Jira parent-child type rules.

    Returns an error string if invalid, None if valid.

    INTERNAL to this repo's own task tool — the sole caller is
    handle_create_scaffolded, validating input it is about to store itself.
    It is deliberately not wired to any gate: gating on the bare tool name
    applied this rule to other servers' tasks__create, and task hierarchy is
    task-framework's decision, not this repo's. Retire this with src/tools/tasks.py.
    """
    issue_type = (issue_type or "task").lower()
    parent_id  = (parent_id or "").strip()

    if issue_type == "epic":
        if parent_id:
            return "Epics cannot have a parent. Remove parent_id or change issue_type."
        return None

    required_parents = _JIRA_RULES.get(issue_type)
    if required_parents is None:
        return None  # unknown type — pass through

    if not parent_id:
        suggestions = ""
        try:
            from src.tools.tasks import _connect
            placeholders = ", ".join("?" for _ in required_parents)
            with _connect() as conn:
                rows = conn.execute(
                    f"SELECT id, title FROM open_tasks WHERE issue_type IN ({placeholders}) "
                    f"AND status='open' ORDER BY created_at DESC LIMIT 5",
                    tuple(required_parents),
                ).fetchall()
            if rows:
                candidates = "; ".join(f"{r['id']}: {r['title']}" for r in rows)
                suggestions = f" Candidates: {candidates}."
        except Exception as exc:
            _log.warning("[validate_jira_hierarchy] candidate lookup failed: %s", exc)
        return (
            f"issue_type='{issue_type}' requires a parent "
            f"(must be: {', '.join(sorted(required_parents))}). "
            f"Select a parent or change the type.{suggestions}"
        )

    try:
        from src.tools.tasks import _connect
        with _connect() as conn:
            row = conn.execute(
                "SELECT issue_type FROM open_tasks WHERE id=?", (parent_id,)
            ).fetchone()
    except Exception as exc:
        _log.warning("[validate_jira_hierarchy] DB lookup failed: %s — failing open", exc)
        return None

    if row is None:
        _log.warning("[validate_jira_hierarchy] parent_id=%s not found", parent_id)
        return f"Parent task '{parent_id}' not found."

    parent_type = (row["issue_type"] or "task").lower()
    _log.info("[validate_jira_hierarchy] issue_type=%s parent_id=%s parent_type=%s required=%s",
              issue_type, parent_id, parent_type, sorted(required_parents))
    if parent_type not in required_parents:
        return (
            f"issue_type='{issue_type}' requires a parent of type "
            f"{', '.join(sorted(required_parents))}, "
            f"but '{parent_id}' is a '{parent_type}'."
        )

    return None


# JiraHierarchyGate was removed here. It registered under the bare name
# tasks__create, which more than one MCP server provides, so it enforced this
# repo's hierarchy rule over task-framework's — a framework that owns the rule
# itself and states a DIFFERENT one (an epic may not have a parent; a task
# needs none). Task hierarchy is task-framework's decision to make; a gate here
# was a second, contradictory home for it. handle_create_scaffolded still
# validates its own input directly, which is this repo checking its own data.



# ---------------------------------------------------------------------------
# Gate registry
# ---------------------------------------------------------------------------

# The task lifecycle gates are gone. They enforced task-framework's state
# machine from here, reading this repo's open_tasks, which made a taskfw id
# look like a missing task and a taskfw rule look like this repo's to set.
# Both commit gates stay: neither touches a task store — one regexes the Bash
# command for task:<id>, the other checks a typed param — so traceability
# survives the extraction with no dependency on taskfw at all.
GATES: dict[str, Gate] = {g.tool_name: g for g in [
    GitCommitGate(),
    GitCommitMcpGate(),
]}

# Merge external gates from gate_rules.yaml — external entries never override internal ones
GATES = {**_load_external_gates(), **GATES}


def check(tool_short_name: str, ctx: GateContext) -> tuple[bool, str]:
    """Dispatch to the gate for tool_short_name, if one exists.

    Returns (deny, reason):
        deny=False  → tool is allowed (not gated, or gate satisfied)
        deny=True   → tool must be blocked; reason is the message for Claude
    """
    gate = GATES.get(tool_short_name)
    if gate is None:
        _log.debug("[gates.check] tool=%s not_gated → allow", tool_short_name)
        return False, ""
    return gate.verify(ctx)


