"""Shared text utilities for node scoring."""
from __future__ import annotations

import re
import sqlite3
from typing import Optional

_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can",
    "had", "her", "was", "one", "our", "out", "day", "get", "has",
    "him", "his", "how", "its", "may", "now", "use", "way", "who",
    "did", "let", "put", "say", "she", "too", "any", "via", "per",
    "that", "this", "with", "they", "will", "from", "been", "have",
    "than", "when", "also", "into", "what", "which", "here", "just",
    "then", "them", "some", "more", "make", "like", "time", "only",
    "each", "does", "over", "such", "used", "both", "very", "even",
    "most", "made", "after", "where", "being", "other", "these",
    "their", "there", "about", "would", "could", "should", "those",
})


def tokenise(text: str) -> set[str]:
    """Return set of meaningful lowercase tokens (3+ chars, non-stop-words) from text."""
    return {t for t in re.findall(r"[a-z]{3,}", text.lower()) if t not in _STOP_WORDS}


# task_project_tag was removed here (task:6240c675). It read a task's
# project:<name> tag out of proj_tasks.db, a store this repo no longer owns,
# and the tag itself was derived from a working directory.

