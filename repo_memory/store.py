"""RepoMemoryStore — JSON-backed store for repo-specific project/reference memories.

Sibling to concept_store/store.py's ConceptStore: same {"meta": ..., "<key>": {...}}
wrapper shape, same tolerant-missing-file behavior, same upsert/get/list/delete/search
API. Kept as an independent class rather than sharing a base with ConceptStore — same
shape today, different validation rules (type enum) and likely-diverging fields later.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DEFAULT_FILENAME = "memories.json"
_VALID_TYPES = ("project", "reference")


def resolve_path(cwd: str) -> Optional[Path]:
    """Path to <cwd>/repo_memory/memories.json if it exists, else None.

    Same convention for every project — no per-repo config. A repo's presence
    of this file is the switch consumers use to decide whether to prefer
    repo-local memories over the global MEMORY.sqlite store for that cwd.
    """
    if not cwd:
        return None
    path = Path(cwd) / "repo_memory" / _DEFAULT_FILENAME
    return path if path.exists() else None


class RepoMemoryStore:
    """Stores repo-specific project/reference memories as a JSON file keyed by name.

    Each memory:
        name           — unique slug
        type           — "project" | "reference"
        body           — the memory content
        tags           — free-text keywords
        files          — comma-separated file paths this memory relates to
        related        — comma-separated related memory name slugs
        last_validated — ISO timestamp
        created_at     — ISO timestamp

    No `domain` field — the repo path itself is the domain. No `hit_count`/
    `last_hit` — this store is queried directly during task lifecycle
    (grooming/introspection/activation), never scored per-turn by the UPS
    retrieval pipeline, so there is nothing to record hits against.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self._meta: dict = {"commit": "", "extracted_at": "", "note": ""}
        if self._path.exists():
            text = self._path.read_text(encoding="utf-8").strip()
            raw = json.loads(text) if text else {}
            if "memories" in raw:
                self._data = raw["memories"]
                self._meta.update(raw.get("meta", {}))
            else:
                # Legacy flat layout ({name: memory}), same tolerance as ConceptStore.
                self._data = raw

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert(self, memory: dict) -> None:
        name = memory["name"]
        mem_type = memory.get("type", "")
        if mem_type not in _VALID_TYPES:
            raise ValueError(f"type must be one of {_VALID_TYPES}, got {mem_type!r}")
        now = datetime.now(timezone.utc).isoformat()
        existing = self._data.get(name, {})
        self._data[name] = {
            "name":           name,
            "type":           mem_type,
            "body":           memory.get("body", ""),
            "tags":           memory.get("tags", ""),
            "files":          memory.get("files", ""),
            "related":        memory.get("related", ""),
            "last_validated": now,
            "created_at":     existing.get("created_at", now),
        }
        self.save()

    def delete(self, name: str) -> None:
        self._data.pop(name, None)
        self.save()

    def save(self) -> None:
        payload = {"meta": self._meta, "memories": self._data}
        self._path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def set_meta(self, **fields) -> None:
        self._meta.update(fields)
        self.save()

    @property
    def meta(self) -> dict:
        return dict(self._meta)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[dict]:
        return self._data.get(name)

    def list(self, type: Optional[str] = None) -> list[dict]:
        memories = list(self._data.values())
        if type is not None:
            memories = [m for m in memories if m.get("type") == type]
        return memories

    def search(self, query: str) -> list[dict]:
        """Keyword search across name/body/tags.

        Case-insensitive substring match. Ranks by number of matching fields,
        most matches first, ties broken by name.
        """
        needle = query.lower().strip()
        if not needle:
            return []

        scored: list[tuple[int, dict]] = []
        for memory in self._data.values():
            score = 0
            if needle in memory.get("name", "").lower():
                score += 1
            if needle in memory.get("body", "").lower():
                score += 1
            if needle in memory.get("tags", "").lower():
                score += 1
            if score:
                scored.append((score, memory))

        scored.sort(key=lambda pair: (-pair[0], pair[1].get("name", "")))
        return [m for _, m in scored]

    def __len__(self) -> int:
        return len(self._data)
