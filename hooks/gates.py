"""Send-gate policy — lookup-before-send enforcement.

Single source of truth for which tools are gated and what prerequisites they
require. Completely independent of DB state — operates purely on GateContext (in-memory dataclass).

Adding a gate: add a Gate subclass below + register it in GATES. Gates for
other repos' MCP tools live with those tools, not here.

Anti-hallucination principle: Claude cannot be trusted to remember whether it
already verified something. Only tool call records in prompt_tool_calls (written
by the hook infrastructure, not the model) are facts. Gates enforce this.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

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

import re as _re
import shlex as _shlex
import subprocess as _subprocess

_TASK_ID_RE = _re.compile(r'task:[a-f0-9]{6,}')

# Commit detection asks a question about each shell segment's COMMAND WORD,
# not about whether "git" and "commit" both appear somewhere in the blob.
#
# The previous regex — git\s+(?:(?!commit\b)\S+\s+)*commit\b — had an unbounded
# middle group over \S+\s+, and \s matches newlines, so it bridged any distance
# between a `git` token and a later `commit` token: across newlines, &&, ;, and
# shell comments. `git status` followed by `echo "nothing to commit"` was denied,
# as was a read-only script that merely contained the words as string literals.
# That contradicted this gate's own contract that non-commit calls pass through
# (task:e247268e).
_SEGMENT_SPLIT_RE = _re.compile(r'\n|;|&&|\|\||\||\(|\)|&')

#: git global flags that consume the FOLLOWING token as their value. The `--x=y`
#: spellings need no entry — the value rides along in the same token.
_GIT_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree",
                              "--exec-path", "--namespace"})

#: Wrappers that may precede git without changing what is being run.
_COMMAND_WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "command", "exec"})

#: Matches a leading VAR=value environment assignment.
_ENV_ASSIGN_RE = _re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def _segment_is_git_commit(segment: str) -> bool:
    """True when this one shell segment actually invokes a commit."""
    try:
        tokens = _shlex.split(segment, comments=True)
    except ValueError:
        # Unbalanced quotes are routine here: heredocs and $(...) fragments get
        # split mid-quote. Degrade to a dumb split rather than raise — this gate
        # denies Bash, so an escaping exception would break every Bash call.
        tokens = segment.split()

    i = 0
    while i < len(tokens) and (_ENV_ASSIGN_RE.match(tokens[i]) or tokens[i] in _COMMAND_WRAPPERS):
        i += 1
    if i >= len(tokens):
        return False

    # git_local.sh is a commit tool in its own right — match it on the command
    # word, never as a substring, or `echo "git_local.sh"` resurrects the bug.
    command_word = tokens[i].rsplit("/", 1)[-1].lower()
    if command_word == "git_local.sh":
        return True
    if command_word != "git":
        return False

    # First non-flag argument is the subcommand.
    j = i + 1
    while j < len(tokens):
        token = tokens[j]
        if token in _GIT_VALUE_FLAGS:
            j += 2
        elif token.startswith("-"):
            j += 1
        else:
            return token.lower() == "commit"
    return False


def _is_git_commit(command: str) -> bool:
    """True when any segment of `command` invokes a commit."""
    return any(_segment_is_git_commit(seg) for seg in _SEGMENT_SPLIT_RE.split(command))
# `git commit -F <path>` / `--file <path>` — the form task-framework's own
# /commit skill recommends for multi-paragraph messages, since heredocs and
# -m chains mangle them. The task id then lives in the file, not the command.
_FILE_FLAG_RE = _re.compile(r'(?:-F|--file)(?:=|\s+)("[^"]*"|\'[^\']*\'|\S+)')


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


#: `git commit --amend` — reuses HEAD's message unless a new one is supplied.
_AMEND_RE = _re.compile(r'(?:^|\s)--amend(?:\s|$)')

#: `git -C <dir>` — names the repository when it is not the process cwd.
_DASH_C_RE = _re.compile(r'(?:^|\s)-C(?:=|\s+)("[^"]*"|\'[^\']*\'|\S+)')


def _expand_path(raw: str) -> str:
    """Expand ~ and environment variables in a path the gate was handed.

    The gate inspects the command string BEFORE the shell runs it, so a path
    written as $VAR/msg.txt arrives literally. Expanding here recovers the file
    the command would actually have opened. Anything still unexpanded (a shell
    variable this process does not have, or a $(...) substitution) is left as
    it is and will fail to open — reported, not silently skipped.
    """
    return os.path.expanduser(os.path.expandvars(raw))


def _repo_dir_from_command(command: str) -> str | None:
    """The `git -C <dir>` target, if the command names one."""
    match = _DASH_C_RE.search(command)
    return _expand_path(_strip_quotes(match.group(1))) if match else None


def _head_commit_message(repo_dir: str | None) -> str:
    """HEAD's full commit message, or '' if it cannot be read.

    Fails open by design: this only ever ADDS a reason to allow. A repo with no
    commits, a bad path, or a missing git binary all return '' and leave the
    deny decision exactly where it was.
    """
    try:
        args = ["git"]
        if repo_dir:
            args += ["-C", repo_dir]
        args += ["log", "-1", "--format=%B"]
        result = _subprocess.run(args, capture_output=True, timeout=5, check=False)
        return result.stdout.decode(errors="replace") if result.returncode == 0 else ""
    except Exception:
        return ""


class GitCommitGate(Gate):
    """Gate for Bash tool calls that contain a git commit.

    Passes through all non-commit bash calls immediately. For commit calls,
    denies if no task:<id> pattern is found in the command string OR in a
    file the command passes via `-F`/`--file`. This enforces traceability —
    every commit must reference an active task.
    """
    tool_name = "Bash"

    def verify(self, ctx: GateContext) -> tuple[bool, str]:
        command: str = ctx.tool_input.get("command", "")
        if not _is_git_commit(command):
            _log.debug("[Bash] non-commit bash — allow")
            return False, ""
        if _TASK_ID_RE.search(command):
            _log.info("[Bash] git commit with task:<id> in command — allow")
            return False, ""

        # Paths the gate was told to read but could not. Reported in the deny
        # reason rather than swallowed: a -F file that fails to open looks
        # identical to a -F file with no task id, and only one of those is the
        # author's mistake (task:ad9cae1c).
        unreadable: list[str] = []

        for match in _FILE_FLAG_RE.finditer(command):
            raw = _strip_quotes(match.group(1))
            path = _expand_path(raw)
            try:
                text = Path(path).read_text()
            except OSError as exc:
                unreadable.append(f"{raw} ({exc.strerror or type(exc).__name__})")
                continue
            if _TASK_ID_RE.search(text):
                _log.info("[Bash] git commit with task:<id> in -F file %s — allow", path)
                return False, ""

        # --amend reuses the message already on HEAD when no new one is given,
        # so the id can be real and simply not present anywhere the gate can
        # see. Ask git for it rather than denying a compliant amend.
        if _AMEND_RE.search(command):
            existing = _head_commit_message(_repo_dir_from_command(command))
            if existing and _TASK_ID_RE.search(existing):
                _log.info("[Bash] git commit --amend reusing HEAD message with task:<id> — allow")
                return False, ""

        reason = (
            "Blocked: git commit is missing a task:<id> reference. "
            "Add 'task:<id>' to the commit message body (or the file passed via -F), "
            "or activate a task first with tasks__set_active."
        )
        if unreadable:
            # The gate reads the command as written, before the shell expands
            # it, so a -F path built from a shell variable is opened literally.
            reason += (
                " NOTE: could not read the file(s) passed via -F: "
                + "; ".join(unreadable)
                + ". The gate sees the command before the shell expands it, so"
                " pass -F a literal path rather than one built from a shell"
                " variable or command substitution."
            )
        return True, reason


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


# Jira hierarchy validation retired here (task:87ec7876), with its sole caller
# handle_create_scaffolded — deleted along with src/tools/tasks.py. JiraHierarchyGate
# itself went earlier (task:6240c675): it registered under the bare name
# tasks__create, which more than one MCP server provides, so it enforced this
# repo's hierarchy rule over task-framework's, which owns a DIFFERENT one (an
# epic may not have a parent; a task needs none). Task hierarchy is
# task-framework's decision, not this repo's, and this repo no longer has any
# task hierarchy of its own left to validate.


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


