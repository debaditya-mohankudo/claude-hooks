"""Top banner text: the active taskfw task (id + title) for the focused window's cwd.

Read from ~/.taskfw/active.json, which taskfw writes on every activation; entries whose owning server pid is dead are skipped. With no task active in this cwd it shows the project and the latest prompt of the
newest Claude Code session there (read from its transcript), then the window title.
"""
import json
import os

from kitty.boss import get_boss

ACTIVE_FILE = os.path.expanduser("~/.taskfw/active.json")


PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
MAX_PROMPT = 100
_prompt_cache = {}   # transcript path -> (mtime, latest prompt)


def _window_cwd():
    tab = get_boss().active_tab_manager.active_tab
    return (tab.get_cwd_of_active_window() if tab is not None else None) or ""


def _last_prompt(path):
    """Latest typed prompt of a Claude Code transcript: the last user message whose content is plain text."""
    last = ""
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            text = d.get("message", {}).get("content") if d.get("type") == "user" else None
            if isinstance(text, str) and not d.get("isMeta") and not text.lstrip().startswith("<"):
                last = " ".join(text.split())
    return last


def _session_prompt():
    """(project, latest prompt) of the newest Claude Code session for the focused window's cwd, or None."""
    try:
        d = _window_cwd()
        while d and d != os.path.dirname(d):
            folder = os.path.join(PROJECTS_DIR, d.replace("/", "-").replace(".", "-"))
            if os.path.isdir(folder):
                files = [os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".jsonl")]
                if files:
                    newest = max(files, key=os.path.getmtime)
                    mtime = os.path.getmtime(newest)
                    cached = _prompt_cache.get(newest)
                    if cached is None or cached[0] != mtime:
                        cached = _prompt_cache[newest] = (mtime, _last_prompt(newest))
                    if cached[1]:
                        return os.path.basename(d), cached[1]
                return None
            d = os.path.dirname(d)
    except Exception:
        pass
    return None


def _owner_alive(entry):
    """False only when the entry names a pid that no longer exists (a dead server's leftover)."""
    pid = entry.get("pid")
    if not isinstance(pid, int):
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _active_task():
    try:
        with open(ACTIVE_FILE) as f:
            active = json.load(f)
        d = _window_cwd()
        while d:
            if d in active and _owner_alive(active[d]):
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
    prompt = _session_prompt()
    if prompt:
        text = prompt[1] if len(prompt[1]) <= MAX_PROMPT else prompt[1][:MAX_PROMPT - 1] + "…"
        return f" ◇ {prompt[0]}  {text} "
    title = data.get("title", "") if isinstance(data, dict) else ""
    return f" {title} "
