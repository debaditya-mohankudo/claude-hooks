"""One advisory note when a skill's action runs without the skill (task:df16e538).

`git add`/`git commit` without /commit, `mac memory add` without /add-memory and
`mac vault write` without /vault-save each earn one note per session. The note
never blocks or approves a call. What a session has loaded is kept here, apart
from `_skill_memories._SKILLS_RUN`: that list drives the skill-tag nudge, which
typed slash commands must not change.

Both records are in memory only, so a server restart can repeat a note once.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path

from hooks.core.mac_command import parse_bash
from hooks.gates import _GIT_VALUE_FLAGS
from langchain_learning.nodes._skill_memories import TAG_PREFIX, skill_tag
from src.logger import get_logger

_log = get_logger(__name__)

_MAX_SESSIONS = 200
# session -> {skill name: whether its SKILL.md prescribes a vault write}
_LOADED: OrderedDict[str, dict[str, bool]] = OrderedDict()
_SHOWN: OrderedDict[str, set[str]] = OrderedDict()

_MAC_SKILL = {
    "mac memory add": "add-memory",
    "mac memory add_batch": "add-memory",
    "mac vault write": "vault-save",
    "mac vault append": "vault-save",
}
_VAULT_WRITE = re.compile(r"mac vault (write|append)\b")
# A whole heredoc: `<<EOF`, the rest of that line, the body, the closing line.
_HEREDOC = re.compile(
    r"(?<!<)<<-?[ \t]*\\?(['\"]?)([A-Za-z_][\w.-]*)(?![\w.-])\1([^\n]*)\n(?:.*?\n)??[ \t]*\2[ \t]*(?=\n|$)", re.S)
_NO_EFFECT_FLAGS = {"--help", "-h", "--dry-run"}
# Words that run the command after them.
_WRAPPERS = {"sudo", "nohup", "time", "env", "nice", "exec", "command",
             "if", "then", "else", "do", "while", "until", "!", "{"}

_NOTES = {
    "commit": "`git add`/`git commit` is running and /commit is not loaded in this session.",
    "add-memory": "`mac memory add` is running and /add-memory is not loaded in this session.",
    "vault-save": "A `mac vault` write is running and /vault-save is not loaded in this session.",
}


def _bucket(d: OrderedDict, session_id: str, empty):
    value = d.setdefault(session_id, empty())
    d.move_to_end(session_id)
    while len(d) > _MAX_SESSIONS:
        d.popitem(last=False)
    return value


def _prescribes_vault_write(name: str, cwd: str) -> bool:
    roots = [Path.home()]
    if cwd:
        roots += [Path(cwd), *Path(cwd).parents]
    for root in roots:
        try:
            text = (root / ".claude" / "skills" / name / "SKILL.md").read_text(errors="ignore")
        except OSError:
            continue
        if _VAULT_WRITE.search(text):
            return True
    return False


def note_loaded(session_id: str, skill: str, cwd: str = "") -> None:
    """Record a skill loaded by a Skill call or a typed command. Never raises."""
    try:
        name = skill_tag(skill).removeprefix(TAG_PREFIX)
        # A "/" left over means the prompt began with a file path, not a command.
        if not session_id or not name or "/" in name:
            return
        _bucket(_LOADED, session_id, dict)[name] = _prescribes_vault_write(name, cwd)
    except Exception as exc:
        _log.warning("skill reminder: recording %r failed: %s", skill, exc)


def note_prompt(session_id: str, prompt: str, cwd: str = "") -> None:
    """A prompt starting `/name` loads that skill without any Skill tool call."""
    words = (prompt or "").split(maxsplit=1)
    if words and words[0].startswith("/"):
        note_loaded(session_id, words[0], cwd)


def _git_subcommand(words: list[str]) -> str:
    """`add`, `commit`, ... for one simple command running git, else ""."""
    i = 0
    while i < len(words) and (words[i] in _WRAPPERS or re.match(r"[A-Za-z_]\w*=", words[i])):
        i += 1
    if i >= len(words) or words[i].rsplit("/", 1)[-1] != "git":
        return ""
    i += 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in _GIT_VALUE_FLAGS else 1
    if i >= len(words) or _NO_EFFECT_FLAGS.intersection(words[i + 1:]):
        return ""
    return words[i]


def _wanted(command: str, shell_commands) -> list[str]:
    """The skills whose action this command line performs, in a stable order."""
    # Bodies go first: inside "$(cat <<'EOF' ...)" an odd quote in the text
    # otherwise unbalances the line and hides the command from both splitters.
    if "git" not in command and "mac" not in command:
        return []
    command = _HEREDOC.sub(r"\3", command)
    wanted = set()
    if "git" in command and any(_git_subcommand(w) in ("add", "commit") for w in shell_commands(command)):
        wanted.add("commit")
    for call in parse_bash(command):
        if call.id in _MAC_SKILL and not _NO_EFFECT_FLAGS.intersection(call.args):
            wanted.add(_MAC_SKILL[call.id])
    return [s for s in _NOTES if s in wanted]


def reminder(session_id: str, command: str, shell_commands) -> str:
    """The note this Bash command earns, or "". Marks it shown. Never raises.

    `shell_commands` is the dispatcher's quote- and heredoc-aware splitter.
    """
    try:
        if not session_id:
            return ""
        loaded = _LOADED.get(session_id, {})
        shown = _SHOWN.get(session_id, set())
        due = [s for s in _wanted(command, shell_commands)
               if s not in loaded and s not in shown
               and not (s == "vault-save" and any(loaded.values()))]
        if not due:
            return ""
        _bucket(_SHOWN, session_id, set).update(due)
        _log.info("skill reminder: session=%s skills=%s", session_id[:8], due)
        return "\n".join(
            f"{_NOTES[s]} Load the `{s}` skill now and apply its steps; if the command has "
            "already run, check its result against them. Ignore this if skipping it was deliberate."
            for s in due)
    except Exception as exc:
        _log.warning("skill reminder failed: %s", exc)
        return ""
