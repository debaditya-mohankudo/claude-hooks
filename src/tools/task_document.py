"""Typed Python representation of the task `document` JSON column (epic:f42b6958).

Plain dataclasses with to_json()/from_json(), not pydantic — matches this
repo's existing convention (concept_store.store.ConceptStore uses plain
dicts, no model-validation layer) rather than introducing new machinery for
a shape that may still evolve. See task:74dad096 for the design rationale
behind each field.

Adopted (epic:f42b6958): wired into src/tools/tasks.py's tasks__update_document
(task:544a21c0), deployed to test, and in active use by /task-grooming and
/task-introspection.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

SCHEMA_VERSION = 1

# risks[].graded values a completed grooming risk can be assigned at
# introspection time — "missed" is deliberately NOT here, see IntrospectionReport.
_VALID_GRADES = {"materialized", "avoided", "wrong"}


@dataclass
class Risk:
    """One risk/hidden-assumption grooming predicted, graded later by introspection."""
    text: str
    graded: Optional[str] = None

    def __post_init__(self) -> None:
        if self.graded is not None and self.graded not in _VALID_GRADES:
            raise ValueError(f"Risk.graded must be one of {_VALID_GRADES} or None, got {self.graded!r}")


@dataclass
class Grooming:
    """Latest grooming pass only — not an array. Re-grooming overwrites this
    wholesale; if history is ever wanted, add grooming_history[] as a new
    key later (schema_version bump), don't retrofit an array here."""
    last_run_at: Optional[str] = None
    clarifications: list[str] = field(default_factory=list)
    hidden_assumptions: list[str] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)
    prior_art: list[str] = field(default_factory=list)
    suggested_improvements: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Grooming":
        return cls(
            last_run_at=data.get("last_run_at"),
            clarifications=list(data.get("clarifications", [])),
            hidden_assumptions=list(data.get("hidden_assumptions", [])),
            risks=[Risk(**r) for r in data.get("risks", [])],
            prior_art=list(data.get("prior_art", [])),
            suggested_improvements=list(data.get("suggested_improvements", [])),
        )

    def grade_risks(self, grades_by_text: dict[str, str]) -> list[str]:
        """Set .graded on existing risks matched by exact text, WITHOUT
        touching last_run_at or any other field — this is /task-introspection
        grading risks /task-grooming already wrote, not a new grooming pass
        (task:3c46c40d found handle_update_document's wholesale grooming
        replace would wrongly bump last_run_at if reused for this).

        Returns the list of grades_by_text keys that matched no risk (so the
        caller can surface "graded a risk that no longer exists" rather than
        silently dropping it).
        """
        unmatched = list(grades_by_text.keys())
        for risk in self.risks:
            if risk.text in grades_by_text:
                grade = grades_by_text[risk.text]
                if grade not in _VALID_GRADES:
                    raise ValueError(f"grade must be one of {_VALID_GRADES}, got {grade!r}")
                risk.graded = grade
                unmatched.remove(risk.text)
        return unmatched


@dataclass
class GroomingAccuracy:
    """Step 3.0's tally — counts, not the graded Risk objects themselves
    (those live in the Grooming that was graded, findable via last_run_at)."""
    predicted: int = 0
    materialized: int = 0
    avoided: int = 0
    wrong: int = 0


@dataclass
class IntrospectionReport:
    date: str
    grooming_accuracy: GroomingAccuracy = field(default_factory=GroomingAccuracy)
    missed_surprises: list[str] = field(default_factory=list)
    new_knowledge: list[str] = field(default_factory=list)
    stale_knowledge_flagged: list[str] = field(default_factory=list)
    highest_leverage: str = ""
    overall_assessment: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "IntrospectionReport":
        return cls(
            date=data["date"],
            grooming_accuracy=GroomingAccuracy(**data.get("grooming_accuracy", {})),
            missed_surprises=list(data.get("missed_surprises", [])),
            new_knowledge=list(data.get("new_knowledge", [])),
            stale_knowledge_flagged=list(data.get("stale_knowledge_flagged", [])),
            highest_leverage=data.get("highest_leverage", ""),
            overall_assessment=data.get("overall_assessment", ""),
        )


@dataclass
class Introspection:
    reports: list[IntrospectionReport] = field(default_factory=list)


@dataclass
class Implementation:
    """Quantified deliverable for /task-implementation (task:4d3a6fc5) — a
    derived tally of the body's Resolution: checklist, not a copy of it. The
    checklist itself stays in the body (single source of truth, human-skimmable
    in the IDE); this is recomputed on every handle_update body write, always
    overwritten wholesale (no history to preserve, unlike Grooming)."""
    total: int = 0
    done: int = 0


@dataclass
class Related:
    """Reference-only — every list here is a slug/id pointing into a store
    that already owns the real data (commit_task_map, MEMORY.sqlite,
    concept_store/concepts.json, task_events). Never full content."""
    commits: list[str] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    decision_event_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Related":
        return cls(
            commits=list(data.get("commits", [])),
            memories=list(data.get("memories", [])),
            concepts=list(data.get("concepts", [])),
            decision_event_ids=list(data.get("decision_event_ids", [])),
        )

    def merge(self, other: dict) -> None:
        """Extend + dedupe each list in place from a partial update dict —
        both /task-grooming and /task-introspection can add to this
        independently, so this is additive, never a wholesale overwrite."""
        for key in ("commits", "memories", "concepts", "decision_event_ids"):
            if key not in other:
                continue
            existing = getattr(self, key)
            for item in other[key]:
                if item not in existing:
                    existing.append(item)


@dataclass
class TaskDocument:
    schema_version: int = SCHEMA_VERSION
    grooming: Grooming = field(default_factory=Grooming)
    introspection: Introspection = field(default_factory=Introspection)
    implementation: Implementation = field(default_factory=Implementation)
    related: Related = field(default_factory=Related)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def to_dict(self) -> dict:
        """Plain dict, e.g. for embedding directly in an MCP tool response
        without a round-trip through JSON string serialization."""
        return asdict(self)

    @classmethod
    def from_json(cls, raw: str | None) -> "TaskDocument":
        """NULL/empty document column -> a fresh empty TaskDocument, never an error.

        Every open_tasks row has no document until something writes one —
        this is the required NULL-handling contract from task:74dad096's
        backward-compatibility finding.
        """
        if not raw:
            return cls()
        data = json.loads(raw)

        grooming_data = data.get("grooming") or {}
        introspection_data = data.get("introspection") or {}
        implementation_data = data.get("implementation") or {}
        related_data = data.get("related") or {}

        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            grooming=Grooming.from_dict(grooming_data),
            introspection=Introspection(
                reports=[
                    IntrospectionReport.from_dict(r)
                    for r in introspection_data.get("reports", [])
                ]
            ),
            implementation=Implementation(
                total=implementation_data.get("total", 0),
                done=implementation_data.get("done", 0),
            ),
            related=Related.from_dict(related_data),
        )
