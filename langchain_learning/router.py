"""Per-turn model/effort router — pure scoring, no I/O.

Hooks cannot switch the model or effort for a turn (UserPromptSubmit may only
block or add context), so this produces a *recommendation* that is injected as
advisory context and logged; route_store.py records what actually ran so the
recommendation can be graded over time. Nothing here acts on the decision.

Tiers map to models in TIER_MODEL. Scoring is additive over cheap lexical
signals; thresholds are module constants so the report can show which ones to
move.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TIER_MODEL = {
    "small":  "claude-haiku-5-5",
    "medium": "claude-sonnet-5-5",
    "large":  "claude-opus-5-5",
}
TIER_EFFORT = {"small": "low", "medium": "medium", "large": "high"}

# score < SMALL_MAX -> small, score >= LARGE_MIN -> large, else medium
SMALL_MAX = 2
LARGE_MIN = 6

_HARD = re.compile(
    r"\b(debug|root cause|why (is|does|did|isn'?t|doesn'?t)|race condition|deadlock|"
    r"architect|design|refactor|migrate|migration|trade-?offs?|investigate|"
    r"plan|strategy|security|audit|review|concurrency|performance|optimi[sz]e)\b", re.I)
_EDIT = re.compile(
    r"\b(implement|add|build|create|write|fix|change|update|rename|remove|delete)\b", re.I)
_LOOKUP = re.compile(
    r"^\s*(what|where|which|who|when|show|list|print|open|read|cat|run|status|"
    r"yes|no|ok|okay|sure|thanks|thank you|go ahead|continue|do it|lgtm)\b", re.I)
_CODE_FENCE = re.compile(r"```")
_PATH = re.compile(r"(?:[\w.-]+/)+[\w.-]+\.\w+|\b\w+\.(?:py|ts|js|json|md|sql|sysml|swift)\b")


@dataclass(frozen=True)
class Decision:
    tier: str
    model: str
    effort: str
    score: int
    reasons: list[str] = field(default_factory=list)

    def advisory(self) -> str:
        return (f"Router suggests {self.model} at {self.effort} effort "
                f"(tier={self.tier}, score={self.score}: {', '.join(self.reasons) or 'no signals'}). "
                f"Advisory only — hooks cannot switch the model.")


def route(prompt: str, *, turn: int = 0, prev_tool_count: int = 0) -> Decision:
    """Score a prompt and return the recommended tier. Never raises."""
    text = (prompt or "").strip()
    score = 0
    reasons: list[str] = []

    def add(n: int, why: str) -> None:
        nonlocal score
        score += n
        reasons.append(f"{why}{n:+d}")

    words = len(text.split())
    if words > 150:
        add(3, "long")
    elif words > 40:
        add(1, "medium-length")

    if _CODE_FENCE.search(text):
        add(1, "code-block")
    paths = len(set(_PATH.findall(text)))
    if paths >= 3:
        add(2, "many-files")
    elif paths >= 1:
        add(1, "file-ref")

    hard = len(set(m.group(0).lower() for m in _HARD.finditer(text)))
    if hard:
        add(min(hard, 3) * 2, "hard-words")
    if _EDIT.search(text):
        add(1, "edit")
    if text.count("?") >= 3:
        add(1, "multi-question")

    if prev_tool_count >= 15:
        add(1, "prev-turn-tool-heavy")

    if words <= 12 and _LOOKUP.match(text) and not hard:
        add(-2, "short-lookup")

    tier = "small" if score < SMALL_MAX else "large" if score >= LARGE_MIN else "medium"
    return Decision(tier, TIER_MODEL[tier], TIER_EFFORT[tier], score, reasons)
