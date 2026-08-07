"""ScoreToolsNode — retrieves relevant tool hints from tool_hints.sqlite."""
from __future__ import annotations

from langchain_learning.nodes._node_log import entry
from langchain_learning.nodes._text_utils import tokenise
from langchain_learning.retrievers import KeywordOverlapScorer, ToolScorer
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)


class ScoreToolsNode:
    """Retrieve top-5 tool hints by keyword overlap, tie-broken by usage count.

    score = kw_overlap + log1p(count) * 0.3   (only when kw_overlap > 0 — see task:53c9f817)
    where kw_overlap = count of prompt keywords that appear in the tool's keywords column,
    and count is the tool's real invocation count (mcp_tool_hints.count).

    Accepts an optional ToolScorer at construction time; defaults to
    KeywordOverlapScorer (production backend). Pass NullToolScorer or a custom
    stub in tests to avoid requiring a real SQLite fixture.

    Tags: tool-hints, scoring-pipeline, BM25, keyword-overlap, tool-suggestion
    """

    def __init__(self, scorer: ToolScorer | None = None) -> None:
        self._scorer: ToolScorer = scorer if scorer is not None else KeywordOverlapScorer()

    def __call__(self, state: SessionState) -> dict:
        entry("score_tools", state)

        keywords = set(state.get("keywords") or tokenise(state.get("prompt", "").lower()))

        try:
            hints = self._scorer.score(keywords)
        except Exception as exc:
            _log.error("[score_tools] scorer error: %s", exc)
            return {"tool_hints": []}

        _log.info("[score_tools] returned=%d tools=%s", len(hints), [h["tool_name"] for h in hints])
        return {"tool_hints": hints}
