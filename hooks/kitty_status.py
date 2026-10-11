"""Kitty tab status for a Claude Code session, driven by hook events.

Called from client.py, which runs as a child of the Claude session and so
inherits KITTY_WINDOW_ID / KITTY_LISTEN_ON. The long-running hook server has
neither, which is why this lives client-side.

Contract:
- Maps a hook event to one of three states (working / needs_input / done) and
  paints the tab that contains this session's window (match window_id:N), so
  concurrent sessions never touch each other's tabs.
- SessionStart / SessionEnd clear the tab's title and colours.
- A tab launched for a taskfw task (TASKFW_TASK_ID in the environment, set by
  kitty-taskfw at launch) keeps the title it was launched with: only colours are
  painted there, never the title, so the task id is not replaced by "claude".
- Never raises, never blocks: `kitten @` is spawned detached with output
  discarded and is not waited on. Without kitty, a socket, or the `kitten`
  binary (or with CLAUDE_KITTY_STATUS=0) it does nothing.
"""
import os
import shutil
import subprocess

# state -> (glyph, tab background, tab foreground)
_STATES = {
    "working":     ("●", "#d29922", "#000000"),
    "needs_input": ("▲", "#f85149", "#ffffff"),
    "done":        ("✓", "#3fb950", "#000000"),
}

_EVENT_STATE = {
    "UserPromptSubmit": "working",
    "PreToolUse":       "working",
    "PostToolUse":      "working",
    "Stop":             "done",
    "Notification":     "needs_input",
}
_CLEAR_EVENTS = {"SessionStart", "SessionEnd"}


def state_for(event: str, payload: dict | None = None) -> str | None:
    """State name for a hook event, "clear" to reset the tab, or None to leave it."""
    if event in _CLEAR_EVENTS:
        return "clear"
    if event == "Notification" and (payload or {}).get("notification_type") == "idle_prompt":
        return "done"
    return _EVENT_STATE.get(event)


def _commands(window_id: str, state: str) -> list[list[str]]:
    match = ["--match", f"window_id:{window_id}"]
    title_is_ours = not os.environ.get("TASKFW_TASK_ID")
    if state == "clear":
        cmds = [["set-tab-color", *match, "active_bg=NONE", "active_fg=NONE",
                 "inactive_bg=NONE", "inactive_fg=NONE"]]
        return [["set-tab-title", *match, ""], *cmds] if title_is_ours else cmds
    glyph, bg, fg = _STATES[state]
    cmds = [["set-tab-color", *match, f"active_bg={bg}", f"active_fg={fg}",
             f"inactive_bg={bg}", f"inactive_fg={fg}"]]
    return [["set-tab-title", *match, f"{glyph} claude"], *cmds] if title_is_ours else cmds


def update(event: str, payload: dict | None = None) -> None:
    try:
        if os.environ.get("CLAUDE_KITTY_STATUS") == "0":
            return
        socket = os.environ.get("KITTY_LISTEN_ON")
        window_id = os.environ.get("KITTY_WINDOW_ID")
        if not socket or not window_id:
            return
        state = state_for(event, payload)
        if state is None:
            return
        kitten = shutil.which("kitten")
        if not kitten:
            return
        for args in _commands(window_id, state):
            subprocess.Popen(
                [kitten, "@", "--to", socket, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass
