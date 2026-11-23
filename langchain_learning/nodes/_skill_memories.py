"""Skill-tagged memories (task:85801ef4).

A memory tagged `skill-<name>` is surfaced when the Skill tool runs `<name>`,
by exact tag match (not BM25), and the write side is nudged to tag new memories
saved during a skill run. The hook only ever reads and reminds; it never edits a command.
"""
from __future__ import annotations

import re
import sqlite3
from collections import OrderedDict

from src.logger import get_logger

_log = get_logger(__name__)

TAG_PREFIX = "skill-"
DEFAULT_CAP = 3
_MAX_SESSIONS = 50

_PROMPT_INJECTED: OrderedDict[str, set[str]] = OrderedDict()   # session -> names injected for the current prompt
_SKILLS_RUN: OrderedDict[str, list[str]] = OrderedDict()        # session -> skill tags run, oldest first


def skill_tag(skill: str) -> str:
    """`skill-<name>` for a Skill-tool name; a plugin prefix ('plugin:name') is dropped."""
    name = (skill or "").strip().lstrip("/").rsplit(":", 1)[-1].lower()
    return TAG_PREFIX + name if name else ""


def tag_tokens(tags: str | None) -> set[str]:
    return {t for t in re.split(r"[,\s]+", (tags or "").lower()) if t}


def _bounded(d: OrderedDict, key: str):
    d.move_to_end(key)
    while len(d) > _MAX_SESSIONS:
        d.popitem(last=False)


def note_prompt_injected(session_id: str, names: list[str]) -> None:
    """Called per prompt by LoadMemoriesNode: replaces the session's set, so only this prompt's names count."""
    if not session_id:
        return
    _PROMPT_INJECTED[session_id] = set(names)
    _bounded(_PROMPT_INJECTED, session_id)


def note_skill_run(session_id: str, tag: str) -> None:
    if not session_id or not tag:
        return
    _SKILLS_RUN.setdefault(session_id, []).append(tag)
    _bounded(_SKILLS_RUN, session_id)


def last_skill_tag(session_id: str) -> str:
    runs = _SKILLS_RUN.get(session_id) or []
    return runs[-1] if runs else ""


def lookup(conn: sqlite3.Connection, tag: str, cap: int = DEFAULT_CAP, skip: set[str] | None = None) -> list[dict]:
    """Memories carrying exactly `tag`, newest then most-hit first, superseded ones dropped, `skip` names removed."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, type, tags, body, updated, hit_count FROM memories WHERE lower(tags) LIKE ?",
        (f"%{tag}%",),
    ).fetchall()
    rows = [r for r in rows if tag in tag_tokens(r["tags"]) and r["name"] not in (skip or ())]
    if not rows:
        return []
    ph = ",".join("?" * len(rows))
    try:
        old = {r[0] for r in conn.execute(
            f"SELECT dst_id FROM memory_links WHERE type='supersedes' AND dst_id IN ({ph})",
            [r["id"] for r in rows],
        )}
    except sqlite3.Error:
        old = set()
    rows = [r for r in rows if r["id"] not in old]
    rows.sort(key=lambda r: (r["updated"] or "", r["hit_count"] or 0), reverse=True)
    top = [dict(r) for r in rows[:cap]]
    try:
        from langchain_learning.nodes._memory_scoring import _mark_conflicts
        _mark_conflicts(conn, top)
    except Exception as exc:   # marking is advisory
        _log.warning("skill memories: conflict marking failed: %s", exc)
    return top


def format_block(tag: str, memories: list[dict]) -> str:
    lines = [f"## Memories for {tag}"]
    for m in memories:
        lines.append(f"### {m['name']} [{m.get('type', '')}]")
        if m.get("conflicts_with"):
            lines.append(f"> conflicts with: {', '.join(m['conflicts_with'])}")
        body = (m.get("body") or "").strip()
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines).strip()


def inject_for_skill(session_id: str, skill: str) -> dict | None:
    """PostToolUse output for a Skill call, or None. Never raises."""
    tag = skill_tag(skill)
    if not tag:
        return None
    note_skill_run(session_id, tag)
    try:
        from langchain_learning.config import config as _cfg
        from langchain_learning.memory_conn import memory_connection
        if not _cfg.memory_db.exists():
            return None
        with memory_connection(_cfg.memory_db) as conn:
            mems = lookup(conn, tag, skip=_PROMPT_INJECTED.get(session_id))
    except Exception as exc:
        _log.warning("skill memories lookup failed (%s): %s", tag, exc)
        return None
    if not mems:
        return None
    names = [m["name"] for m in mems]
    try:
        from langchain_learning.nodes import _memory_audit
        _memory_audit.record(session_id, f"skill:{tag}", set(), [], [], names)
    except Exception as exc:
        _log.warning("skill memories audit failed: %s", exc)
    try:
        from langchain_learning.nodes._memory_scoring import record_memory_hits
        record_memory_hits(names)
    except Exception as exc:
        _log.warning("skill memories hit count failed: %s", exc)
    _log.info("skill memories: %s injected=%s", tag, names)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": format_block(tag, mems)}}


_WRITE_IDS = ("mac memory add", "mac memory add_batch")


def tag_nudge(session_id: str, call_ids: list[str], command: str) -> dict | None:
    """Reminder when a memory is saved after a skill ran and the command carries no skill-* tag.

    For add_batch the tags live in a JSON file, so only the command line is checked.
    """
    tag = last_skill_tag(session_id)
    if not tag or not any(c in _WRITE_IDS for c in call_ids) or TAG_PREFIX in (command or ""):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
        f"Memory saved without a skill-* tag, and `{tag[len(TAG_PREFIX):]}` ran earlier in this session. "
        f"If the memory belongs to that skill, re-add it with `{tag}` in --tags (tags are merged, so "
        "existing ones are kept); otherwise ignore this."
    )}}
