"""SysML model tests — the model checked mechanically, not just asserted.

Ported from task-framework's tests/test_models.py (task:79d7aa34), which makes
single-allocation its acceptance criterion. The port is deliberate: claude-hooks
had five .sysml files and NOTHING referencing them, so they drifted for a month
into confident-sounding fiction — a requirement citing a deleted module, a
requirement modelling a gate that no longer exists, a parts list off by three
domains. A criterion nobody runs is a comment.

Two checks matter more here than in the original, because they match how this
repo's models actually decayed:

  * Citations. The drift was overwhelmingly dead POINTERS — src/tools/tasks.py,
    commit_task_map, a retired gate — inside docs that still parsed perfectly.
    A structure-only test would have caught none of it, so every requirement
    must carry a `Source:` line and every file path on it must exist.

  * Live counts. MCPTools claimed "9 domains / 56 actions" long after the real
    DOMAIN_MAP fell to 6/25. Asserting the number against src/dispatcher.py
    turns the model into a tripwire on the code rather than prose that rots.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
MODELS = REPO / "models"

EXPECTED = {
    "foundation.sysml",
    "claude_hooks_system.sysml",
    "requirements.sysml",
    "user_prompt_submit_flow.sysml",
}

REQUIREMENT_DEF = re.compile(r"requirement\s+def\s+(\w+)")
SATISFY = re.compile(r"satisfy\s+requirement\s+(\w+)\s+by\s+([\w.]+)\s*;")
PART_DEF = re.compile(r"part\s+def\s+(\w+)")
PART_INSTANCE = re.compile(r"part\s+(\w+)\s*:\s*(\w+)\s*;")

#: A `Source:` line inside a requirement doc, up to the end of that line.
SOURCE_LINE = re.compile(r"Source:([^\n*]*(?:\n\s*\*[^\n]*)*)")
#: Anything that looks like a repo-relative file path.
PATH_LIKE = re.compile(r"\b((?:[\w.-]+/)+[\w.-]+\.(?:py|json|sysml|md|yaml|yml))\b")


@pytest.fixture(scope="module")
def requirements() -> str:
    return (MODELS / "requirements.sysml").read_text()


@pytest.fixture(scope="module")
def system() -> str:
    return (MODELS / "claude_hooks_system.sysml").read_text()


class TestPresence:
    def test_model_files_are_exactly_the_expected_set(self):
        assert {p.name for p in MODELS.glob("*.sysml")} == EXPECTED

    def test_models_live_at_repo_root_not_under_docs(self):
        """Guards the relocation in commit 1a062d8. A test still globbing
        docs/models/ would find zero files and pass vacuously — the exact
        failure mode this file exists to prevent."""
        assert MODELS.is_dir()
        assert not (REPO / "docs" / "models").exists()


class TestSingleAllocation:
    def test_no_requirement_has_more_than_one_satisfy(self, requirements):
        """THE acceptance criterion. A second allocation means a rule and its
        data have drifted apart."""
        counts: dict[str, list[str]] = {}
        for name, part in SATISFY.findall(requirements):
            counts.setdefault(name, []).append(part)
        duplicates = {n: p for n, p in counts.items() if len(p) > 1}
        assert not duplicates, (
            f"requirements allocated to more than one part: {duplicates}"
        )

    def test_every_requirement_is_allocated(self, requirements):
        defined = set(REQUIREMENT_DEF.findall(requirements))
        allocated = {name for name, _ in SATISFY.findall(requirements)}
        assert defined == allocated, (
            f"unallocated: {sorted(defined - allocated)}; "
            f"allocated but undefined: {sorted(allocated - defined)}"
        )

    def test_allocations_name_real_parts(self, requirements, system):
        composed = {name for name, _ in PART_INSTANCE.findall(system)}
        for requirement, target in SATISFY.findall(requirements):
            assert target.startswith("system."), f"{requirement} allocated to {target}"
            part = target.split(".", 1)[1]
            assert part in composed, f"{requirement} allocated to unknown part {part!r}"


class TestCitations:
    """Every requirement must cite where it came from, and the citation must
    still exist. This is the check that would have caught the real drift."""

    def test_every_requirement_doc_has_a_source_line(self, requirements):
        blocks = requirements.split("requirement def ")[1:]
        missing = [b.split("{")[0].strip() for b in blocks if "Source:" not in b.split("satisfy")[0]]
        assert not missing, f"requirements with no Source: line: {missing}"

    def test_cited_paths_exist(self, requirements):
        dead = sorted({
            path
            for source in SOURCE_LINE.findall(requirements)
            for path in PATH_LIKE.findall(source)
            if not (REPO / path).exists()
        })
        assert not dead, (
            f"requirement docs cite files that no longer exist: {dead}"
        )


class TestPartsMatchCode:
    def test_the_parts_are_defined(self, system):
        assert set(PART_DEF.findall(system)) == {
            "HookServer", "MCPTools", "MemoryConceptRAGStores",
            "LangGraphPipeline", "System",
        }

    def test_retired_task_subsystem_has_not_reappeared(self, system):
        """Task tracking left this repo (task:87ec7876, commit 060e823) and
        lives in task-framework. Re-adding a part for it would re-model a
        subsystem this repo does not own.

        Checks STRUCTURE, not prose. The word TaskGraph is expected to appear —
        the package doc names it in a DELIBERATELY ABSENT note, because saying
        what was removed and why is how this repo keeps a deletion from being
        silently undone. A test that banned the word would forbid the tombstone
        and reward deleting the explanation.
        """
        assert "part def TaskGraph" not in system
        assert "TaskGraph" not in {t for _n, t in PART_INSTANCE.findall(system)}

    def test_mcp_domain_count_matches_dispatcher(self, system):
        """A live tripwire on src/dispatcher.py. The model said 9 domains / 56
        actions for a month after the real numbers fell to 6 / 25."""
        from src.dispatcher import DOMAIN_MAP

        domains = len(DOMAIN_MAP)
        actions = sum(len(actions) for _module, actions in DOMAIN_MAP.values())
        block = system.split("part def MCPTools")[1].split("part def")[0]
        assert f"{domains} domains" in block, (
            f"model does not say '{domains} domains' — DOMAIN_MAP has {domains}"
        )
        assert f"{actions} actions" in block, (
            f"model does not say '{actions} actions' — DOMAIN_MAP has {actions}"
        )
