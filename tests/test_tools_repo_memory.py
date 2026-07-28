"""Tests for src/tools/repo_memory.py — repo_memory__* MCP tools wrapping the
JSON-format RepoMemoryStore for repo-specific project/reference memories."""
import json

import pytest

from src.tools.repo_memory import (
    handle_delete,
    handle_get,
    handle_list,
    handle_search,
    handle_upsert,
)


@pytest.fixture
def repo(tmp_path):
    """A bare repo dir with no repo_memory/ at all — upsert must create it."""
    return str(tmp_path)


@pytest.fixture
def repo_with_store(tmp_path):
    store_dir = tmp_path / "repo_memory"
    store_dir.mkdir()
    (store_dir / "memories.json").write_text(json.dumps({"meta": {}, "memories": {}}))
    return str(tmp_path)


def _memory(name="foo-bar", type="project", **overrides):
    base = {
        "name": name,
        "type": type,
        "body": "does foo things",
        "tags": "foo, bar",
        "files": "foo.py",
        "related": "",
    }
    base.update(overrides)
    return base


class TestResolveRepo:
    def test_missing_repo_arg_is_error(self):
        assert "error" in handle_get("", "foo")

    def test_nonexistent_repo_path_is_error(self):
        assert "error" in handle_get("/no/such/path/at/all", "foo")


class TestUpsertGet:
    def test_upsert_creates_repo_memory_dir_when_absent(self, repo):
        result = handle_upsert(repo, _memory())
        assert result == {"ok": True, "name": "foo-bar"}
        got = handle_get(repo, "foo-bar")
        assert got["found"] is True
        assert got["memory"]["name"] == "foo-bar"

    def test_upsert_missing_name_is_error(self, repo):
        result = handle_upsert(repo, {"type": "project", "body": "x"})
        assert "error" in result

    def test_upsert_invalid_type_is_error(self, repo):
        result = handle_upsert(repo, _memory(type="feedback"))
        assert "error" in result

    def test_get_missing_memory_not_found(self, repo_with_store):
        assert handle_get(repo_with_store, "nope") == {"found": False}

    def test_upsert_preserves_created_at_across_updates(self, repo):
        handle_upsert(repo, _memory(body="v1"))
        first = handle_get(repo, "foo-bar")["memory"]
        handle_upsert(repo, _memory(body="v2"))
        second = handle_get(repo, "foo-bar")["memory"]
        assert first["created_at"] == second["created_at"]
        assert second["body"] == "v2"
        assert second["last_validated"] >= first["last_validated"]


class TestList:
    def test_list_empty_store(self, repo_with_store):
        assert handle_list(repo_with_store) == {"memories": []}

    def test_list_filters_by_type(self, repo):
        handle_upsert(repo, _memory(name="a", type="project"))
        handle_upsert(repo, _memory(name="b", type="reference"))
        result = handle_list(repo, type="reference")
        assert len(result["memories"]) == 1
        assert result["memories"][0]["name"] == "b"

    def test_list_no_filter_returns_all(self, repo):
        handle_upsert(repo, _memory(name="a"))
        handle_upsert(repo, _memory(name="b"))
        result = handle_list(repo)
        assert len(result["memories"]) == 2


class TestDelete:
    def test_delete_existing(self, repo):
        handle_upsert(repo, _memory())
        result = handle_delete(repo, "foo-bar")
        assert result == {"ok": True, "deleted": True}
        assert handle_get(repo, "foo-bar") == {"found": False}

    def test_delete_missing_is_noop_not_error(self, repo_with_store):
        assert handle_delete(repo_with_store, "nope") == {"ok": True, "deleted": False}


class TestSearch:
    def test_missing_repo_arg_is_error(self):
        assert "error" in handle_search("", "foo")

    def test_empty_query_is_error(self, repo_with_store):
        assert "error" in handle_search(repo_with_store, "")

    def test_search_by_body(self, repo):
        handle_upsert(repo, _memory(name="a", body="does foo things"))
        handle_upsert(repo, _memory(name="b", body="unrelated", tags="baz"))
        result = handle_search(repo, "foo")
        assert len(result["memories"]) == 1
        assert result["memories"][0]["name"] == "a"

    def test_search_no_match_returns_empty_list(self, repo_with_store):
        assert handle_search(repo_with_store, "nonexistent") == {"memories": []}
