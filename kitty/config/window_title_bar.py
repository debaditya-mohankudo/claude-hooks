"""Top banner text: the active taskfw task (id + title) for the focused window's cwd.

Read from ~/.taskfw/active.json, which taskfw writes on every activation. Falls back to the
window title when no task is active in this cwd.
"""
import json
import os

from kitty.boss import get_boss

ACTIVE_FILE = os.path.expanduser("~/.taskfw/active.json")


def _active_task():
    try:
        with open(ACTIVE_FILE) as f:
            active = json.load(f)
        tab = get_boss().active_tab_manager.active_tab
        d = (tab.get_cwd_of_active_window() if tab is not None else None) or ""
        while d:
            if d in active:
                return active[d]["id"], active[d].get("title", "")
            if d == os.path.dirname(d):
                break
            d = os.path.dirname(d)
    except Exception:
        pass
    return None


def draw_window_title(data):
    task = _active_task()
    if task:
        return f" ◆ {task[0]}  {' '.join((task[1] or '').split())} "
    title = data.get("title", "") if isinstance(data, dict) else ""
    return f" {title} "
