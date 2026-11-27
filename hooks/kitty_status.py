"""Kitty tab status for a Claude Code session, driven by hook events.

Called from client.py, which runs as a child of the Claude session and so
inherits KITTY_WINDOW_ID / KITTY_LISTEN_ON. The long-running hook server has
neither: for the PostToolUse "http" hook it passes `env`, built from the
request headers (server.py _HEADER_ENV, task:90637166).

Contract:
- Maps a hook event to one of three states (working / needs_input / waiting) and
  paints the tab that contains this session's window (match window_id:N), so
  concurrent sessions never touch each other's tabs.
- SessionEnd clears the tab's title and colours.
- A tab launched for a taskfw task (TASKFW_TASK_ID in the environment, set by
  kitty-taskfw at launch) keeps the title it was launched with: only colours are
  painted there, never the title, so the task id is not replaced by "claude".
- Never raises, never blocks: `kitten @` is spawned detached with output
  discarded and is not waited on. Without kitty, a socket, or the `kitten`
  binary (or with CLAUDE_KITTY_STATUS=0) it does nothing.

Vocabulary (task:b5c1e08c, user decision 2026-11-25): "waiting" means the
session wants the user's input, and it is the same word taskfw's kitty hook
(taskfw/kitty/hook.py EVENT_STATE) writes into the `state` user var for the
same events. Stop used to be called "done" here and painted a green check,
which read as "finished, nothing needed" while taskfw called the same event
"waiting" — one event, two opposite-sounding names. The two repos stay
decoupled: neither reads the other's signal, only the words agree. "done"
now survives in taskfw alone, where it means the session has ended, which is
what SessionEnd clears the tab for here.
"""
import os
import shutil
import subprocess
from collections.abc import Mapping

# The launchd server's PATH has no /opt/homebrew/bin, so PATH lookup alone fails there.
_KITTEN_FALLBACKS = ("/opt/homebrew/bin/kitten", "/Applications/kitty.app/Contents/MacOS/kitten")


def _kitten() -> str | None:
    return shutil.which("kitten") or next(
        (p for p in _KITTEN_FALLBACKS if os.access(p, os.X_OK)), None)


# state -> (glyph, tab background, tab foreground)
# "waiting" is deliberately not a green check (task:b5c1e08c): the turn being
# over is the user's cue to type, not a finished job.
_STATES = {
    "working":     ("●", "#d29922", "#000000"),
    "needs_input": ("▲", "#f85149", "#ffffff"),
    "waiting":     ("◆", "#58a6ff", "#000000"),
}

_EVENT_STATE = {
    "SessionStart":     "waiting",
    "UserPromptSubmit": "working",
    "PreToolUse":       "working",
    "PostToolUse":      "working",
    "Stop":             "waiting",
    "Notification":     "needs_input",
}
# SessionStart no longer clears (task:b5c1e08c): a fresh session is already
# waiting for its first prompt, and saying so matches taskfw's SessionStart.
_CLEAR_EVENTS = {"SessionEnd"}


def state_for(event: str, payload: dict | None = None) -> str | None:
    """State name for a hook event, "clear" to reset the tab, or None to leave it."""
    if event in _CLEAR_EVENTS:
        return "clear"
    if event == "Notification" and (payload or {}).get("notification_type") == "idle_prompt":
        return "waiting"  # idling is the user's turn, not a permission prompt
    return _EVENT_STATE.get(event)


def _commands(window_id: str, state: str, env: Mapping[str, str]) -> list[list[str]]:
    match = ["--match", f"window_id:{window_id}"]
    title_is_ours = not env.get("TASKFW_TASK_ID")
    if state == "clear":
        cmds = [["set-tab-color", *match, "active_bg=NONE", "active_fg=NONE",
                 "inactive_bg=NONE", "inactive_fg=NONE"]]
        return [["set-tab-title", *match, ""], *cmds] if title_is_ours else cmds
    glyph, bg, fg = _STATES[state]
    cmds = [["set-tab-color", *match, f"active_bg={bg}", f"active_fg={fg}",
             f"inactive_bg={bg}", f"inactive_fg={fg}"]]
    return [["set-tab-title", *match, f"{glyph} claude"], *cmds] if title_is_ours else cmds


def update(event: str, payload: dict | None = None, env: Mapping[str, str] | None = None) -> None:
    """`env` is the session's environment; None means this process's own."""
    try:
        env = os.environ if env is None else env
        if env.get("CLAUDE_KITTY_STATUS") == "0":
            return
        socket = env.get("KITTY_LISTEN_ON")
        window_id = env.get("KITTY_WINDOW_ID")
        if not socket or not window_id:
            return
        state = state_for(event, payload)
        if state is None:
            return
        kitten = _kitten()
        if not kitten:
            return
        for args in _commands(window_id, state, env):
            subprocess.Popen(
                [kitten, "@", "--to", socket, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass
