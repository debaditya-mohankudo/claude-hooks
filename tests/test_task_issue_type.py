"""Tests for issue_type column on open_tasks — create, update, validation."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import src.tools.tasks as tasks_module
from src.tools.tasks import handle_create, handle_get, handle_list, handle_update
from src.db.schema import OPEN_TASKS_DDL, TASK_EVENTS_DDL, TASK_EDGES_DDL
from src.tools.task_document import TaskDocument


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db = tmp_path / "proj_tasks.db"
    with patch("src.tools.tasks._DB", db):
        yield db


class TestCreateIssueType:
    def test_default_is_task(self):
        r = handle_create(title="My task", body="Type: feature\nTask: x\nResolution: y\nMotivation: z\nFiles: f")
        assert r["issue_type"] == "task"

    def test_explicit_epic(self):
        r = handle_create(title="Big epic", body="Type: feature\nTask: x\nResolution: y\nMotivation: z\nFiles: f", issue_type="epic")
        assert r["issue_type"] == "epic"

    def test_all_valid_types(self):
        for itype in ("epic", "story", "task", "bug", "subtask"):
            r = handle_create(title=f"t-{itype}", body="Type: feature\nTask: x\nResolution: y\nMotivation: z\nFiles: f", issue_type=itype)
            assert r["issue_type"] == itype

    def test_invalid_type_returns_error(self):
        r = handle_create(title="bad", body="Type: feature\nTask: x\nResolution: y\nMotivation: z\nFiles: f", issue_type="sprint")
        assert "error" in r


class TestAutoFillFromTemplate:
    def test_empty_body_auto_fills_misc_template(self):
        r = handle_create(title="Fix the flaky test")
        assert "error" not in r
        assert "id" in r

    def test_empty_body_with_task_type_uses_that_template(self):
        r = handle_create(title="Investigate slow queries", task_type="research")
        assert "error" not in r
        assert "id" in r

    def test_auto_filled_body_contains_title_and_type_line(self):
        r = handle_create(title="Fix the flaky test")
        row = handle_get(r["id"])
        assert "Type: misc" in row["body"]
        assert "Fix the flaky test" in row["body"]

    def test_unknown_task_type_returns_error(self):
        r = handle_create(title="x", task_type="not-a-real-type")
        assert "error" in r

    def test_explicit_body_is_not_overwritten(self):
        body = "Type: feature\nTask: x\nResolution: y\nMotivation: z\nFiles: f"
        r = handle_create(title="Has explicit body", body=body, task_type="misc")
        row = handle_get(r["id"])
        assert row["body"] == body


class TestUpdateIssueType:
    def _create(self, issue_type="task"):
        return handle_create(
            title="base task",
            body="Type: feature\nTask: x\nResolution: y\nMotivation: z\nFiles: f",
            issue_type=issue_type,
        )["id"]

    def test_update_issue_type(self):
        tid = self._create()
        r = handle_update(id=tid, issue_type="bug")
        assert r["issue_type"] == "bug"

    def test_update_preserves_issue_type_when_not_specified(self):
        tid = self._create(issue_type="story")
        r = handle_update(id=tid, title="new title")
        assert r["issue_type"] == "story"

    def test_update_invalid_type_returns_error(self):
        tid = self._create()
        r = handle_update(id=tid, issue_type="invalid")
        assert "error" in r


class TestGetAndListIssueType:
    def test_get_returns_issue_type(self):
        tid = handle_create(
            title="story task",
            body="Type: feature\nTask: x\nResolution: y\nMotivation: z\nFiles: f",
            issue_type="story",
        )["id"]
        r = handle_get(id=tid)
        assert r["issue_type"] == "story"

    def test_list_returns_issue_type(self):
        handle_create(
            title="listed epic",
            body="Type: feature\nTask: x\nResolution: y\nMotivation: z\nFiles: f",
            issue_type="epic",
        )
        rows = handle_list(format="json")
        assert any(t["issue_type"] == "epic" for t in rows)


BODY = "Type: feature\nTask: x\nResolution: y\nMotivation: z\nFiles: f"


class TestParentIdColumn:
    def _mk(self, title, parent_id="", issue_type="task"):
        return handle_create(title=title, body=BODY, parent_id=parent_id, issue_type=issue_type)["id"]

    def test_create_sets_parent_id_column(self):
        epic = self._mk("Epic", issue_type="epic")
        story = self._mk("Story", parent_id=epic, issue_type="story")
        row = handle_get(id=story)
        assert row["parent_id"] == epic

    def test_create_no_parent_has_null(self):
        tid = self._mk("Solo")
        row = handle_get(id=tid)
        assert row["parent_id"] is None

    def test_list_depth_zero_for_roots(self):
        self._mk("Root epic", issue_type="epic")
        rows = handle_list(format="json")
        roots = [r for r in rows if not r.get("parent_id")]
        assert all(r["depth"] == 0 for r in roots)

    def test_list_three_level_tree_order_and_depth(self):
        epic = self._mk("Epic", issue_type="epic")
        story = self._mk("Story", parent_id=epic, issue_type="story")
        subtask = self._mk("Subtask", parent_id=story, issue_type="subtask")
        rows = handle_list(format="json")
        ids = [r["id"] for r in rows]
        depths = {r["id"]: r["depth"] for r in rows}
        # DFS order: epic before story before subtask
        assert ids.index(epic) < ids.index(story) < ids.index(subtask)
        assert depths[epic] == 0
        assert depths[story] == 1
        assert depths[subtask] == 2

    def test_list_all_tasks_have_depth_field(self):
        self._mk("A")
        self._mk("B")
        rows = handle_list(format="json")
        assert all("depth" in r for r in rows)

    def test_migration_backfill_from_tags(self, tmp_path):
        """parent_id column is backfilled from parent:<id> tags on old DBs."""
        import sqlite3 as _sq
        import uuid
        from unittest.mock import patch

        db = tmp_path / "old.db"
        # Create a DB that looks like pre-parent_id schema (no parent_id column)
        conn = _sq.connect(str(db))
        conn.execute("""
            CREATE TABLE open_tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                body TEXT DEFAULT '', tags TEXT DEFAULT '',
                status TEXT DEFAULT 'open', issue_type TEXT DEFAULT 'task',
                created_at TIMESTAMP DEFAULT (datetime('now')),
                updated_at TIMESTAMP DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL, prompt_id TEXT DEFAULT '',
                session_id TEXT DEFAULT '', turn INTEGER DEFAULT 0,
                summary TEXT DEFAULT '', tools TEXT DEFAULT '',
                logged_at TIMESTAMP DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE task_edges (
                from_id TEXT NOT NULL, to_id TEXT NOT NULL,
                relation_type TEXT NOT NULL, created_at TIMESTAMP DEFAULT (datetime('now')),
                PRIMARY KEY (from_id, to_id, relation_type)
            )
        """)
        parent_id = uuid.uuid4().hex[:8]
        child_id = uuid.uuid4().hex[:8]
        conn.execute("INSERT INTO open_tasks (id, title, tags) VALUES (?, ?, ?)", (parent_id, "Parent", ""))
        conn.execute("INSERT INTO open_tasks (id, title, tags) VALUES (?, ?, ?)", (child_id, "Child", f"parent:{parent_id}"))
        conn.commit()
        conn.close()

        with patch("src.tools.tasks._DB", db):
            rows = handle_list(format="json")
        child_row = next(r for r in rows if r["id"] == child_id)
        assert child_row["parent_id"] == parent_id

    def test_cycle_guard_does_not_infinite_loop(self, tmp_path):
        """Cycle in parent_id (A→B→A) must not cause infinite recursion."""
        import sqlite3 as _sq
        import uuid
        from unittest.mock import patch

        db = tmp_path / "cycle.db"
        conn = _sq.connect(str(db))
        conn.executescript(OPEN_TASKS_DDL)
        conn.executescript(TASK_EVENTS_DDL)
        conn.executescript(TASK_EDGES_DDL)
        a, b = uuid.uuid4().hex[:8], uuid.uuid4().hex[:8]
        conn.execute("INSERT INTO open_tasks (id, title, parent_id) VALUES (?, ?, ?)", (a, "A", b))
        conn.execute("INSERT INTO open_tasks (id, title, parent_id) VALUES (?, ?, ?)", (b, "B", a))
        conn.commit()
        conn.close()

        with patch("src.tools.tasks._DB", db):
            rows = handle_list(format="json")
        assert len(rows) == 2  # both returned, no crash


class TestListToonFormat:
    def _mk(self, title, parent_id="", issue_type="task"):
        return handle_create(title=title, body=BODY, parent_id=parent_id, issue_type=issue_type)["id"]

    def test_toon_is_default_format(self):
        self._mk("A")
        result = handle_list()
        assert isinstance(result, str)
        assert result.startswith("count: 1")
        assert "rows[1]{id,title,tags,status,issue_type,parent_id,keywords,created_at,updated_at,groomed_at,introspected_at,depth,_context_only}:" in result

    def test_toon_empty_list(self):
        result = handle_list(status="abandoned")
        assert result == "count: 0\nrows[0]{}:"

    def test_toon_normalizes_context_only_field(self):
        # A context-only parent row (pulled in despite not matching the status
        # filter) must not break the shared TOON header across all rows.
        epic = self._mk("Epic", issue_type="epic")
        self._mk("Story", parent_id=epic, issue_type="story")
        # Filter to a status the epic doesn't have but the story does, forcing
        # the epic to appear as a context-only parent.
        from src.tools.tasks import handle_update
        handle_update(id=epic, status="done")
        result = handle_list(status="open")
        assert "count: 2" in result


class TestGroomedAt:
    def _mk(self, title="A"):
        return handle_create(title=title, body=BODY)["id"]

    def test_default_groomed_at_is_none(self):
        tid = self._mk()
        row = handle_get(id=tid)
        assert row["groomed_at"] is None

    def test_mark_groomed_sets_timestamp(self):
        tid = self._mk()
        result = tasks_module.handle_update_document(id=tid, mark_groomed=True)
        assert result["groomed"] is True
        row = handle_get(id=tid)
        assert row["groomed_at"] is not None

    def test_update_without_mark_groomed_leaves_it_unset(self):
        tid = self._mk()
        handle_update(id=tid, title="renamed")
        row = handle_get(id=tid)
        assert row["groomed_at"] is None

    def test_mark_groomed_does_not_require_other_fields(self):
        tid = self._mk()
        result = tasks_module.handle_update_document(id=tid, mark_groomed=True)
        assert "error" not in result

    def test_list_json_surfaces_groomed_at(self):
        tid = self._mk()
        tasks_module.handle_update_document(id=tid, mark_groomed=True)
        rows = handle_list(format="json")
        row = next(r for r in rows if r["id"] == tid)
        assert row["groomed_at"] is not None

    def test_list_toon_includes_groomed_at_field(self):
        self._mk()
        result = handle_list()
        assert "groomed_at" in result

    def test_migration_adds_groomed_at_to_existing_db(self, tmp_path):
        """A DB predating the groomed_at column gets it added on connect."""
        import sqlite3 as _sq

        db = tmp_path / "old.db"
        conn = _sq.connect(str(db))
        conn.execute("""
            CREATE TABLE open_tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                body TEXT DEFAULT '', tags TEXT DEFAULT '',
                status TEXT DEFAULT 'open', issue_type TEXT DEFAULT 'task',
                created_at TIMESTAMP DEFAULT (datetime('now')),
                updated_at TIMESTAMP DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT INTO open_tasks (id, title) VALUES ('x1', 'Old task')")
        conn.commit()
        conn.close()

        with patch("src.tools.tasks._DB", db):
            row = handle_get(id="x1")
        assert "groomed_at" in row
        assert row["groomed_at"] is None


def _ddl_columns(ddl: str) -> set[str]:
    """Column names sqlite actually creates from a DDL string (via a throwaway
    in-memory DB), rather than regex-parsing the SQL text ourselves."""
    import sqlite3 as _sq

    conn = _sq.connect(":memory:")
    conn.executescript(ddl)
    table = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()[0]
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return cols


class TestSchemaParity:
    """tasks.py's _ensure_db() now executes schema.py's DDL constants and
    calls migrate_tasks_db() directly (task:1310d3a3) — column-name parity
    is guaranteed by construction, not by two independently-maintained
    copies (that was the pre-task:1310d3a3 design, task:cb357eb6/de1ae61).
    These column-equality checks stay as cheap regression guards against a
    future accidental revert to inline duplication; the real regression
    coverage for THIS module's remaining independent logic (backfills,
    status normalization) is in TestBackfillAndNormalization below.
    """

    def test_open_tasks_columns_match(self, tmp_path):
        db = tmp_path / "parity.db"
        with patch("src.tools.tasks._DB", db):
            handle_create(title="x", body=BODY)
        with __import__("sqlite3").connect(str(db)) as conn:
            prod_cols = {row[1] for row in conn.execute("PRAGMA table_info(open_tasks)")}
        assert prod_cols == _ddl_columns(OPEN_TASKS_DDL)

    def test_task_events_columns_match(self, tmp_path):
        db = tmp_path / "parity.db"
        with patch("src.tools.tasks._DB", db):
            handle_create(title="x", body=BODY)
        with __import__("sqlite3").connect(str(db)) as conn:
            prod_cols = {row[1] for row in conn.execute("PRAGMA table_info(task_events)")}
        assert prod_cols == _ddl_columns(TASK_EVENTS_DDL)

    def test_task_edges_columns_match(self, tmp_path):
        db = tmp_path / "parity.db"
        with patch("src.tools.tasks._DB", db):
            handle_create(title="x", body=BODY)
        with __import__("sqlite3").connect(str(db)) as conn:
            prod_cols = {row[1] for row in conn.execute("PRAGMA table_info(task_edges)")}
        assert prod_cols == _ddl_columns(TASK_EDGES_DDL)

    def test_task_edges_table_created_by_ensure_db(self, tmp_path):
        """Regression test for task:9d3acbef: task_edges was missing from
        _ensure_db() entirely, only existing in production because
        scripts/init_db.py happened to be run once, manually."""
        db = tmp_path / "fresh.db"
        with patch("src.tools.tasks._DB", db):
            handle_create(title="x", body=BODY)
        with __import__("sqlite3").connect(str(db)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "task_edges" in tables

    def test_commit_task_map_table_created_by_ensure_db(self, tmp_path):
        """commit_task_map has no DDL constant in schema.py at all pre-task:1310d3a3
        — confirms _ensure_db()'s new schema.py-delegated path still creates it."""
        db = tmp_path / "fresh.db"
        with patch("src.tools.tasks._DB", db):
            handle_create(title="x", body=BODY)
        with __import__("sqlite3").connect(str(db)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "commit_task_map" in tables


class TestBackfillAndNormalization:
    """task:1310d3a3 — regression coverage for the migration behavior that
    stays in tasks.py (not delegated to schema.py) because it depends on
    tasks.py's own helpers: keywords/parent_id backfill for legacy rows,
    and wip/active status normalization. Column existence itself is now
    schema.py's job (migrate_tasks_db()); these tests guard the VALUES."""

    def test_parent_id_backfilled_from_legacy_tags_on_missing_column(self, tmp_path):
        db = tmp_path / "legacy.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
                CREATE TABLE open_tasks (
                    id TEXT PRIMARY KEY, title TEXT, body TEXT DEFAULT '',
                    tags TEXT DEFAULT '', status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT (datetime('now')),
                    updated_at TIMESTAMP DEFAULT (datetime('now'))
                )
            """)
            conn.execute(
                "INSERT INTO open_tasks (id, title, tags) VALUES ('child01', 'Child', 'parent:parent01,other')"
            )
            conn.commit()
        with patch("src.tools.tasks._DB", db):
            handle_get("child01")  # any call through _connect() triggers _ensure_db
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT parent_id FROM open_tasks WHERE id='child01'").fetchone()
        assert row[0] == "parent01"

    def test_keywords_backfilled_on_missing_column(self, tmp_path):
        db = tmp_path / "legacy.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
                CREATE TABLE open_tasks (
                    id TEXT PRIMARY KEY, title TEXT, body TEXT DEFAULT '',
                    tags TEXT DEFAULT '', status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT (datetime('now')),
                    updated_at TIMESTAMP DEFAULT (datetime('now'))
                )
            """)
            conn.execute(
                "INSERT INTO open_tasks (id, title, body) VALUES ('legacy01', 'Fix authentication bug', 'detailed body text')"
            )
            conn.commit()
        with patch("src.tools.tasks._DB", db):
            handle_get("legacy01")
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT keywords FROM open_tasks WHERE id='legacy01'").fetchone()
        assert row[0] and "authentication" in row[0]

    def test_wip_and_active_status_normalized_to_open(self, tmp_path):
        db = tmp_path / "normalize.db"
        with patch("src.tools.tasks._DB", db):
            tid1 = handle_create(title="a", body=BODY)["id"]
            tid2 = handle_create(title="b", body=BODY)["id"]
        with sqlite3.connect(str(db)) as conn:
            conn.execute("UPDATE open_tasks SET status='wip' WHERE id=?", (tid1,))
            conn.execute("UPDATE open_tasks SET status='active' WHERE id=?", (tid2,))
            conn.commit()
        with patch("src.tools.tasks._DB", db):
            handle_get(tid1)  # any _connect() call re-runs _migrate()
        with sqlite3.connect(str(db)) as conn:
            statuses = {row[0] for row in conn.execute("SELECT status FROM open_tasks WHERE id IN (?, ?)", (tid1, tid2))}
        assert statuses == {"open"}


class TestDocumentColumn:
    """epic:f42b6958 — experimental document column. Verifies the column
    itself (added to both tasks.py's real _ensure_db() and schema.py's
    test-fixture DDL, per task:544a21c0), separately from TaskDocument's
    own serialization (covered by tests/test_task_document.py)."""

    def _mk(self, tmp_path, title="A"):
        db = tmp_path / "doc.db"
        with patch("src.tools.tasks._DB", db):
            tid = handle_create(title=title, body=BODY)["id"]
        return db, tid

    def test_new_task_document_defaults_to_none(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            row = handle_get(id=tid)
        assert row["document"] == {}

    def test_get_document_on_fresh_task_is_empty_taskdocument(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            with tasks_module._connect() as conn:
                doc = tasks_module._get_document(conn, tid)
        assert doc == TaskDocument()

    def test_set_then_get_document_round_trips(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            with tasks_module._connect() as conn:
                doc = tasks_module._get_document(conn, tid)
                doc.grooming.last_run_at = "2026-07-26T12:00:00Z"
                doc.related.concepts.append("tasks-db-schema-and-migration")
                tasks_module._set_document(conn, tid, doc)
                conn.commit()

            with tasks_module._connect() as conn:
                reloaded = tasks_module._get_document(conn, tid)
        assert reloaded == doc

        with patch("src.tools.tasks._DB", db):
            row = handle_get(id=tid)
        assert row["document"]["grooming"]["last_run_at"] == "2026-07-26T12:00:00Z"
        assert row["document"]["related"]["concepts"] == ["tasks-db-schema-and-migration"]

    def test_document_not_included_in_list_output(self, tmp_path):
        """document is excluded from _task_row (list/search) the same way
        body already is — only handle_get returns it."""
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            rows = handle_list(format="json")
        assert "document" not in rows[0]

    def test_migration_adds_document_to_existing_db(self, tmp_path):
        """A DB predating the document column gets it added on connect."""
        import sqlite3 as _sq

        db = tmp_path / "old.db"
        conn = _sq.connect(str(db))
        conn.execute("""
            CREATE TABLE open_tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                body TEXT DEFAULT '', tags TEXT DEFAULT '',
                status TEXT DEFAULT 'open', issue_type TEXT DEFAULT 'task',
                created_at TIMESTAMP DEFAULT (datetime('now')),
                updated_at TIMESTAMP DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT INTO open_tasks (id, title) VALUES ('x1', 'Old task')")
        conn.commit()
        conn.close()

        with patch("src.tools.tasks._DB", db):
            row = handle_get(id="x1")
        assert row["document"] == {}


class TestUpdateDocument:
    """tasks__update_document (task:dd87e9f0) — the MCP-exposed merge-update
    entry point /task-grooming and /task-introspection write through."""

    def _mk(self, tmp_path, title="A"):
        db = tmp_path / "doc.db"
        with patch("src.tools.tasks._DB", db):
            tid = handle_create(title=title, body=BODY)["id"]
        return db, tid

    def test_missing_task_is_error(self, tmp_path):
        db, _ = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            result = tasks_module.handle_update_document(id="nonexistent", grooming={"clarifications": ["x"]})
        assert "error" in result

    def test_no_args_is_error(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            result = tasks_module.handle_update_document(id=tid)
        assert "error" in result

    def test_grooming_write_sets_last_run_at_server_side(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            result = tasks_module.handle_update_document(
                id=tid,
                grooming={
                    "clarifications": ["no existing concept covers this"],
                    "risks": [{"text": "schema.py names wrong file", "graded": None}],
                },
            )
        assert result["ok"] is True
        assert result["document"]["grooming"]["last_run_at"] is not None
        assert result["document"]["grooming"]["clarifications"] == ["no existing concept covers this"]
        assert result["document"]["grooming"]["risks"] == [{"text": "schema.py names wrong file", "graded": None}]

    def test_grooming_write_ignores_caller_supplied_last_run_at(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            result = tasks_module.handle_update_document(
                id=tid, grooming={"last_run_at": "1999-01-01T00:00:00Z"}
            )
        assert result["document"]["grooming"]["last_run_at"] != "1999-01-01T00:00:00Z"

    def test_regrooming_replaces_wholesale_not_merges(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            tasks_module.handle_update_document(id=tid, grooming={"clarifications": ["first pass"]})
            result = tasks_module.handle_update_document(id=tid, grooming={"clarifications": ["second pass"]})
        assert result["document"]["grooming"]["clarifications"] == ["second pass"]

    def test_introspection_report_appends_not_replaces(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            tasks_module.handle_update_document(
                id=tid, introspection_report={"date": "2026-07-25", "highest_leverage": "first"}
            )
            result = tasks_module.handle_update_document(
                id=tid, introspection_report={"date": "2026-07-26", "highest_leverage": "second"}
            )
        reports = result["document"]["introspection"]["reports"]
        assert len(reports) == 2
        assert reports[0]["highest_leverage"] == "first"
        assert reports[1]["highest_leverage"] == "second"

    def test_related_merges_and_dedupes(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            tasks_module.handle_update_document(id=tid, related={"concepts": ["concept-a"]})
            result = tasks_module.handle_update_document(
                id=tid, related={"concepts": ["concept-a", "concept-b"], "commits": ["c123"]}
            )
        assert result["document"]["related"]["concepts"] == ["concept-a", "concept-b"]
        assert result["document"]["related"]["commits"] == ["c123"]

    def test_grooming_and_related_can_both_be_written_by_different_calls(self, tmp_path):
        """Simulates /task-grooming writing grooming + related concepts,
        then /task-introspection later adding to related without touching
        grooming."""
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            tasks_module.handle_update_document(
                id=tid,
                grooming={"clarifications": ["c1"]},
                related={"concepts": ["concept-a"]},
            )
            result = tasks_module.handle_update_document(id=tid, related={"memories": ["mem-a"]})
        assert result["document"]["grooming"]["clarifications"] == ["c1"]
        assert result["document"]["related"]["concepts"] == ["concept-a"]
        assert result["document"]["related"]["memories"] == ["mem-a"]

    def test_graded_risks_does_not_bump_last_run_at(self, tmp_path):
        """task:3c46c40d: grading an existing risk (introspection's job) must
        not look like a fresh grooming pass just happened."""
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            first = tasks_module.handle_update_document(
                id=tid, grooming={"risks": [{"text": "schema.py names wrong file", "graded": None}]}
            )
            original_last_run_at = first["document"]["grooming"]["last_run_at"]
            result = tasks_module.handle_update_document(
                id=tid, graded_risks={"schema.py names wrong file": "avoided"}
            )
        assert result["document"]["grooming"]["last_run_at"] == original_last_run_at
        assert result["document"]["grooming"]["risks"][0]["graded"] == "avoided"

    def test_graded_risks_unmatched_text_reported_not_raised(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            tasks_module.handle_update_document(id=tid, grooming={"risks": [{"text": "x", "graded": None}]})
            result = tasks_module.handle_update_document(id=tid, graded_risks={"y": "wrong"})
        assert result["unmatched_risk_grades"] == ["y"]
        assert result["document"]["grooming"]["risks"][0]["graded"] is None


class TestImplementationProgress:
    """document.implementation.progress (task:4d3a6fc5) — auto-derived checklist
    tally recomputed on every handle_update, not a duplicated checklist."""

    def _mk(self, tmp_path, body):
        db = tmp_path / "impl.db"
        with patch("src.tools.tasks._DB", db):
            tid = handle_create(title="Impl test", body=body)["id"]
        return db, tid

    def test_no_checklist_is_zero_zero(self, tmp_path):
        db, tid = self._mk(tmp_path, "Type: task\nJust a plain body, no checklist.")
        with patch("src.tools.tasks._DB", db):
            handle_update(id=tid, tags="noop")
            doc = handle_get(tid)["document"]
        assert doc["implementation"] == {"total": 0, "done": 0}

    def test_partial_checklist_counted(self, tmp_path):
        body = (
            "Resolution:\n"
            "- [x] step one\n"
            "- [x] step two\n"
            "- [ ] step three\n"
            "- [ ] step four\n"
            "- [ ] step five"
        )
        db, tid = self._mk(tmp_path, body)
        with patch("src.tools.tasks._DB", db):
            handle_update(id=tid, tags="noop")
            doc = handle_get(tid)["document"]
        assert doc["implementation"] == {"total": 5, "done": 2}

    def test_fully_done_checklist(self, tmp_path):
        body = "Resolution:\n- [x] a\n- [x] b"
        db, tid = self._mk(tmp_path, body)
        with patch("src.tools.tasks._DB", db):
            handle_update(id=tid, tags="noop")
            doc = handle_get(tid)["document"]
        assert doc["implementation"] == {"total": 2, "done": 2}

    def test_recompute_on_every_update_does_not_touch_other_namespaces(self, tmp_path):
        body = "Resolution:\n- [ ] a\n- [ ] b"
        db, tid = self._mk(tmp_path, body)
        with patch("src.tools.tasks._DB", db):
            tasks_module.handle_update_document(id=tid, grooming={"clarifications": ["kept"]})
            handle_update(id=tid, body="Resolution:\n- [x] a\n- [ ] b")
            doc = handle_get(tid)["document"]
        assert doc["implementation"] == {"total": 2, "done": 1}
        assert doc["grooming"]["clarifications"] == ["kept"]

    def test_fenced_code_block_example_not_counted(self, tmp_path):
        body = (
            "Resolution:\n"
            "- [x] real step\n"
            "\n"
            "Example convention:\n"
            "```\n"
            "- [ ] this is just documentation, not a real checklist item\n"
            "```\n"
        )
        db, tid = self._mk(tmp_path, body)
        with patch("src.tools.tasks._DB", db):
            handle_update(id=tid, tags="noop")
            doc = handle_get(tid)["document"]
        assert doc["implementation"] == {"total": 1, "done": 1}

    def test_duplicate_checklist_text_deduped_not_double_counted(self, tmp_path):
        body = "Resolution:\n- [ ] run tests\n- [ ] run tests"
        db, tid = self._mk(tmp_path, body)
        with patch("src.tools.tasks._DB", db):
            handle_update(id=tid, tags="noop")
            doc = handle_get(tid)["document"]
        assert doc["implementation"] == {"total": 1, "done": 0}

    def test_duplicate_checklist_text_done_wins_on_conflict(self, tmp_path):
        """Same item text appearing both checked and unchecked (stale duplicate)
        counts as ONE item, and as done — not two separate items."""
        body = "Resolution:\n- [x] run tests\n- [ ] run tests"
        db, tid = self._mk(tmp_path, body)
        with patch("src.tools.tasks._DB", db):
            handle_update(id=tid, tags="noop")
            doc = handle_get(tid)["document"]
        assert doc["implementation"] == {"total": 1, "done": 1}


class TestIntrospectedAt:
    """introspected_at column (task:e3a0233b) — mirrors groomed_at, but set
    automatically whenever handle_update_document writes an introspection_report,
    not via a separate mark_introspected flag."""

    def _mk(self, tmp_path, title="A"):
        db = tmp_path / "introspected.db"
        with patch("src.tools.tasks._DB", db):
            tid = handle_create(title=title, body=BODY)["id"]
        return db, tid

    def test_nil_by_default(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            row = handle_get(tid)
        assert row["introspected_at"] is None

    def test_set_after_introspection_report(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            tasks_module.handle_update_document(
                id=tid,
                introspection_report={"date": "2026-07-26", "overall_assessment": "fine"},
            )
            row = handle_get(tid)
        assert row["introspected_at"] is not None

    def test_not_set_by_grooming_only_call(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            tasks_module.handle_update_document(id=tid, grooming={"clarifications": ["c1"]})
            row = handle_get(tid)
        assert row["introspected_at"] is None

    def test_not_set_by_related_only_call(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            tasks_module.handle_update_document(id=tid, related={"concepts": ["c1"]})
            row = handle_get(tid)
        assert row["introspected_at"] is None

    def test_present_in_list_rows(self, tmp_path):
        db, tid = self._mk(tmp_path)
        with patch("src.tools.tasks._DB", db):
            rows = handle_list(format="json")
        assert any("introspected_at" in r for r in rows)
