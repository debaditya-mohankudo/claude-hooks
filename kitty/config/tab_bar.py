"""Powerline tab bar plus a right-aligned active taskfw task: id + title (tab_bar_style custom).

The task is taskfw's exported active pointer (~/.taskfw/active.json) for the focused window's cwd.
"""
import json
import os

from kitty.boss import get_boss
from kitty.fast_data_types import Screen, add_timer
from kitty.tab_bar import (
    DrawData, ExtraData, TabBarData, draw_tab_with_powerline,
)

TASK_FG = (0x7DCFFF << 8) | 2   # Tokyo Night cyan
TASK_BG = (0x292E42 << 8) | 2
ACTIVE_FILE = os.path.expanduser("~/.taskfw/active.json")   # written by taskfw store
MAX_FRAC = 0.4   # share of the bar the task label may take
_timer_id = None


def _active_task():
    """(id, title) exported by taskfw for the focused window's cwd (or a parent), or None."""
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


def _redraw(_timer_id):
    tm = get_boss().active_tab_manager
    if tm is not None:
        tm.mark_tab_bar_dirty()


def draw_tab(
    draw_data: DrawData, screen: Screen, tab: TabBarData,
    before: int, max_title_length: int, index: int, is_last: bool,
    extra_data: ExtraData,
) -> int:
    global _timer_id
    if _timer_id is None:
        _timer_id = add_timer(_redraw, 5.0, True)
    end = draw_tab_with_powerline(
        draw_data, screen, tab, before, max_title_length, index, is_last, extra_data)
    return end
