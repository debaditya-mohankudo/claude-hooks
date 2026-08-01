"""Post-tool-use hook — detect concept drift when source files are edited.

Called by Claude Code after Edit/Write tool calls. Reads the changed file path
from CLAUDE_TOOL_INPUT env var, diffs against concepts.json, prints any drift to stderr.

Exits 0 always — must not block edits.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_STORE_PATH = _REPO_ROOT / "concept_store" / "concepts.json"


def main() -> None:
    raw_input = os.environ.get("CLAUDE_TOOL_INPUT", "")
    if not raw_input:
        return

    try:
        tool_input = json.loads(raw_input)
    except json.JSONDecodeError:
        return

    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    # Normalise to a repo-relative path
    try:
        rel = str(Path(file_path).resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return  # file is outside this repo

    if not rel.endswith(".py"):
        return

    if not _STORE_PATH.exists():
        return  # store not seeded yet — skip silently

    from concept_store.store import ConceptStore
    from concept_store.diff import diff, format_drift

    store = ConceptStore(_STORE_PATH)
    try:
        reports = diff([rel], _REPO_ROOT, store)
    except Exception as exc:
        print(f"[concept-drift] error: {exc}", file=sys.stderr)
        return

    output = format_drift(reports)
    if output:
        print(output, file=sys.stderr)


if __name__ == "__main__":
    main()
