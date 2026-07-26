"""Tests for src/tools/task_document.py — TaskDocument dataclass (epic:f42b6958)."""
import json

import pytest

from src.tools.task_document import (
    Grooming,
    GroomingAccuracy,
    Introspection,
    IntrospectionReport,
    Related,
    Risk,
    TaskDocument,
)


class TestRisk:
    def test_default_graded_is_none(self):
        assert Risk(text="x").graded is None

    def test_valid_grades_accepted(self):
        for grade in ("materialized", "avoided", "wrong"):
            assert Risk(text="x", graded=grade).graded == grade

    def test_invalid_grade_rejected(self):
        with pytest.raises(ValueError):
            Risk(text="x", graded="missed")

    def test_missed_is_not_a_valid_grade(self):
        """missed belongs in IntrospectionReport.missed_surprises, not here —
        it has no corresponding risk entry to attach to."""
        with pytest.raises(ValueError):
            Risk(text="x", graded="missed")


class TestGroomingGradeRisks:
    def test_grades_matching_risk_in_place(self):
        g = Grooming(risks=[Risk(text="schema.py names wrong file")])
        unmatched = g.grade_risks({"schema.py names wrong file": "avoided"})
        assert g.risks[0].graded == "avoided"
        assert unmatched == []

    def test_does_not_touch_last_run_at(self):
        g = Grooming(last_run_at="2026-07-26T12:00:00Z", risks=[Risk(text="x")])
        g.grade_risks({"x": "materialized"})
        assert g.last_run_at == "2026-07-26T12:00:00Z"

    def test_unmatched_text_returned_not_raised(self):
        g = Grooming(risks=[Risk(text="x")])
        unmatched = g.grade_risks({"y": "wrong"})
        assert unmatched == ["y"]
        assert g.risks[0].graded is None

    def test_invalid_grade_raises(self):
        g = Grooming(risks=[Risk(text="x")])
        with pytest.raises(ValueError):
            g.grade_risks({"x": "missed"})


class TestTaskDocumentDefaults:
    def test_empty_document_has_defaults(self):
        doc = TaskDocument()
        assert doc.schema_version == 1
        assert doc.grooming.risks == []
        assert doc.introspection.reports == []
        assert doc.related.commits == []


class TestFromJsonNullHandling:
    def test_none_returns_fresh_document(self):
        assert TaskDocument.from_json(None) == TaskDocument()

    def test_empty_string_returns_fresh_document(self):
        assert TaskDocument.from_json("") == TaskDocument()


class TestRoundTrip:
    def test_full_document_round_trips(self):
        doc = TaskDocument(
            grooming=Grooming(
                last_run_at="2026-07-26T12:00:00Z",
                clarifications=["no existing concept covers this"],
                hidden_assumptions=["assumed schema.py was authoritative"],
                risks=[Risk(text="schema.py names wrong file", graded="avoided")],
                prior_art=["task:d2165715"],
            ),
            introspection=Introspection(
                reports=[
                    IntrospectionReport(
                        date="2026-07-26",
                        grooming_accuracy=GroomingAccuracy(predicted=1, avoided=1),
                        missed_surprises=["task_edges was missing from _ensure_db"],
                        new_knowledge=["schema.py/tasks.py split is deliberate"],
                        stale_knowledge_flagged=[],
                        highest_leverage="add a schema-parity test",
                        overall_assessment="clean task, real lesson",
                    )
                ]
            ),
            related=Related(
                commits=["c8716d0"],
                memories=["verify-production-path-before-accepting-task-premise"],
                concepts=["tasks-db-schema-and-migration"],
                decision_event_ids=[1234],
            ),
        )
        round_tripped = TaskDocument.from_json(doc.to_json())
        assert round_tripped == doc

    def test_to_json_is_valid_json(self):
        doc = TaskDocument()
        parsed = json.loads(doc.to_json())
        assert parsed["schema_version"] == 1

    def test_partial_document_missing_keys_use_defaults(self):
        """A document written by an earlier partial implementation (e.g. only
        grooming ever wrote its key) shouldn't crash on read."""
        raw = json.dumps({"schema_version": 1, "grooming": {"last_run_at": "2026-07-26T12:00:00Z"}})
        doc = TaskDocument.from_json(raw)
        assert doc.grooming.last_run_at == "2026-07-26T12:00:00Z"
        assert doc.grooming.risks == []
        assert doc.introspection.reports == []
        assert doc.related.commits == []
