"""Link a commit to its task when a Bash call runs `git commit` (task:e60e2713).

Called by the server on every http PostToolUse. This module only detects the
commit; the link itself is written by taskfw's own `taskfw-backfill` CLI, so
claude-hooks knows nothing of taskfw's store and taskfw nothing of this hook.
Backfill is idempotent and links only what HEAD's message cites, so a failed
`git commit` re-reading the old HEAD adds nothing wrong.

Limits (`python -m taskfw.backfill` recovers them): only `cd <dir>` and
`git -C <dir>` are followed, not subshells, pushd, `bash -c` or $(...); commits
typed in a terminal are not seen.

Fail open: errors are logged, never raised.
"""
from __future__ import annotations

import os
import subprocess

from hooks.dispatcher import _shell_commands
from hooks.gates import _COMMAND_WRAPPERS, _ENV_ASSIGN_RE, _GIT_VALUE_FLAGS, _expand_path
from src.logger import get_logger

log = get_logger(__name__)

_DEFAULT_BACKFILL = "~/workspace/task-framework/.venv/bin/taskfw-backfill"
#: Cap per subprocess. The link runs after the response, so the hook's timeout does not bound it.
_TIMEOUT_S = 5


def _expand(path: str, cwd: str) -> str:
    return os.path.normpath(os.path.join(cwd, _expand_path(path)))


def commit_dirs(command: str, cwd: str) -> list[str]:
    """Directories in which `command` runs `git commit`, in order, de-duplicated.

    Detection is on the command word, so `echo "git commit"` is not a commit.
    """
    here, found = cwd, []
    for words in _shell_commands(command):
        i = 0
        while i < len(words) and (_ENV_ASSIGN_RE.match(words[i]) or words[i] in _COMMAND_WRAPPERS):
            i += 1
        if i >= len(words):
            continue
        word = words[i].rsplit("/", 1)[-1]
        if word == "cd" and i + 1 < len(words):
            here = _expand(words[i + 1], here)
            continue
        if word != "git":
            continue
        repo, j = here, i + 1
        while j < len(words):
            tok = words[j]
            if tok in _GIT_VALUE_FLAGS:
                if tok == "-C" and j + 1 < len(words):
                    repo = _expand(words[j + 1], repo)
                j += 2
            elif tok.startswith("-"):
                j += 1
            else:
                if tok == "commit" and repo not in found:
                    found.append(repo)
                break
    return found


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          timeout=_TIMEOUT_S)


def _link_head(repo: str) -> None:
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return
    toplevel = top.stdout.strip()
    cmd = [os.path.expanduser(os.environ.get("CLAUDE_HOOKS_BACKFILL_BIN") or _DEFAULT_BACKFILL),
           "--repo", toplevel]
    # A root commit has no HEAD~1; without --since backfill reads that one commit.
    if _git(toplevel, "rev-parse", "--verify", "-q", "HEAD~1").returncode == 0:
        cmd += ["--since", "HEAD~1"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S)
    if out.returncode != 0:
        log.warning("commit_link: backfill failed in %s: %s", toplevel, out.stderr.strip()[-300:])
    else:
        log.info("commit_link: %s: %s", toplevel, out.stdout.strip())


def link(hook_input: dict) -> None:
    """Link HEAD of every repo this Bash call committed in."""
    try:
        if hook_input.get("tool_name") != "Bash":
            return
        command = (hook_input.get("tool_input") or {}).get("command") or ""
        if "commit" not in command or "git" not in command:
            return
        for repo in commit_dirs(command, hook_input.get("cwd") or os.path.expanduser("~")):
            _link_head(repo)
    except Exception as exc:
        log.warning("commit_link failed: %s", exc)
