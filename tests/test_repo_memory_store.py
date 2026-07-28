"""Unit tests for RepoMemoryStore."""
import json
import pytest
from repo_memory.store import RepoMemoryStore

_MEMORY = {
    "name": "dispatcher-is-table-driven",
    "type": "project",
    "body": "Adding a new tool family is one DOMAIN_MAP entry + one tools/<name>.py module.",
    "tags": "dispatcher, domain_map, mcp tools",
    "files": "src/dispatcher.py",
    "related": "",
}


def test_upsert_and_get(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    result = store.get("dispatcher-is-table-driven")
    assert result["body"] == _MEMORY["body"]
    assert result["type"] == "project"
    assert result["files"] == "src/dispatcher.py"


def test_upsert_rejects_invalid_type(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    with pytest.raises(ValueError):
        store.upsert({**_MEMORY, "type": "feedback"})


def test_upsert_replaces(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    updated = {**_MEMORY, "body": "updated body"}
    store.upsert(updated)
    result = store.get("dispatcher-is-table-driven")
    assert result["body"] == "updated body"


def test_upsert_preserves_created_at(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    created_at = store.get("dispatcher-is-table-driven")["created_at"]
    store.upsert({**_MEMORY, "body": "changed"})
    assert store.get("dispatcher-is-table-driven")["created_at"] == created_at


def test_delete(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    store.delete("dispatcher-is-table-driven")
    assert store.get("dispatcher-is-table-driven") is None
    assert len(store) == 0


def test_list_all(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    store.upsert({**_MEMORY, "name": "concept-store-json-format", "type": "reference"})
    assert len(store.list()) == 2


def test_list_filtered_by_type(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    store.upsert({**_MEMORY, "name": "concept-store-json-format", "type": "reference"})
    results = store.list(type="reference")
    assert len(results) == 1
    assert results[0]["name"] == "concept-store-json-format"


def test_persistence(tmp_path):
    path = tmp_path / "memories.json"
    store = RepoMemoryStore(path)
    store.upsert(_MEMORY)
    store2 = RepoMemoryStore(path)
    assert store2.get("dispatcher-is-table-driven") is not None
    assert store2.get("dispatcher-is-table-driven")["type"] == "project"


def test_empty_store_get_returns_none(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    assert store.get("nonexistent") is None


def test_json_file_written(tmp_path):
    path = tmp_path / "memories.json"
    store = RepoMemoryStore(path)
    store.upsert(_MEMORY)
    data = json.loads(path.read_text())
    assert "dispatcher-is-table-driven" in data["memories"]


def test_search_matches_name(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    store.upsert({**_MEMORY, "name": "concept-store-json-format", "type": "reference", "body": "unrelated", "tags": ""})
    results = store.search("dispatcher")
    assert len(results) == 1
    assert results[0]["name"] == "dispatcher-is-table-driven"


def test_search_matches_body_and_tags(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    assert len(store.search("DOMAIN_MAP")) == 1  # body
    assert len(store.search("domain_map")) == 1  # tags (lowercase in tags field)


def test_search_is_case_insensitive(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    assert len(store.search("DISPATCHER")) == 1


def test_search_no_match_returns_empty(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    assert store.search("nonexistent-topic") == []


def test_search_empty_query_returns_empty(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    assert store.search("") == []
    assert store.search("   ") == []


def test_search_ranks_more_matches_first(tmp_path):
    store = RepoMemoryStore(tmp_path / "memories.json")
    store.upsert(_MEMORY)
    store.upsert({
        **_MEMORY,
        "name": "concept-store-json-format",
        "type": "reference",
        "body": "unrelated dispatcher-ish text",
        "tags": "",
    })
    results = store.search("dispatcher")
    assert results[0]["name"] == "dispatcher-is-table-driven"
