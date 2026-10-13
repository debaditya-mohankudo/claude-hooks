import json
import re
import sqlite3
import sys
from pathlib import Path

from config import config
from src.db.schema import MEMORY_LINKS_DDL
from src.logger import get_logger

# Ensure repo root is on path so scripts.build_memories_embeddings resolves
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_log = get_logger(__name__)

# All user-supplied values (name, type, domain, tags, body, query) are passed via
# parameterized queries (? placeholders) — SQL injection is not possible here.

MEMORY_DB      = config.memory_db
TOOL_HINTS_DB  = config.tool_hints_db
VALID_TYPES    = set(config.memory_valid_types)

_NEW_COLUMNS = [
    "ALTER TABLE memories ADD COLUMN last_validated TIMESTAMP",
    "ALTER TABLE memories ADD COLUMN files TEXT",
    "ALTER TABLE memories ADD COLUMN docs TEXT",
    "ALTER TABLE memories ADD COLUMN related TEXT",
]


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Idempotently add new columns — safe to run on every startup."""
    for stmt in _NEW_COLUMNS:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists


def _connect() -> sqlite3.Connection:
    """Connection with FK enforcement on (per-connection in SQLite) and the links schema present."""
    con = sqlite3.connect(MEMORY_DB)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _ensure_links_schema(con: sqlite3.Connection) -> None:
    con.executescript(MEMORY_LINKS_DDL)  # idempotent; commits any open transaction first


def _normalize_links(links) -> tuple[list[tuple[str, str]], str | None]:
    """Parse `links` into [(target_slug, type)]. A bare string means relates_to."""
    out: list[tuple[str, str]] = []
    for item in links or []:
        if isinstance(item, str):
            item = {"to": item}
        if not isinstance(item, dict) or not item.get("to"):
            return [], f"invalid link {item!r}: expected {{'to': slug, 'type': link_type}}"
        out.append((item["to"], item.get("type") or "relates_to"))
    return out, None


def _validate_links(con: sqlite3.Connection, name: str, links: list[tuple[str, str]],
                    batch_names: set[str] = frozenset()) -> str | None:
    """Return an error message if any link is invalid, else None. Targets may be in the same batch."""
    types = {r[0] for r in con.execute("SELECT type FROM edge_types")}
    for target, ltype in links:
        if ltype not in types:
            return f"unknown link type '{ltype}'. Must be one of: {', '.join(sorted(types))}"
        if target == name:
            return f"memory '{name}' cannot link to itself"
        if target not in batch_names and not con.execute(
            "SELECT 1 FROM memories WHERE name = ?", (target,)
        ).fetchone():
            return f"link target '{target}' does not exist"
    return None


def _write_links(con: sqlite3.Connection, name: str, links: list[tuple[str, str]]) -> None:
    """Insert edges (additive, idempotent). Symmetric types are stored once with src_id < dst_id."""
    src = con.execute("SELECT id FROM memories WHERE name = ?", (name,)).fetchone()[0]
    for target, ltype in links:
        dst = con.execute("SELECT id FROM memories WHERE name = ?", (target,)).fetchone()[0]
        symmetric = con.execute("SELECT symmetric FROM edge_types WHERE type = ?", (ltype,)).fetchone()[0]
        a, b = (min(src, dst), max(src, dst)) if symmetric else (src, dst)
        con.execute(
            "INSERT OR IGNORE INTO memory_links (src_id, dst_id, type) VALUES (?, ?, ?)",
            (a, b, ltype),
        )


def _write_related(con: sqlite3.Connection, name: str, related: str) -> list[str]:
    """Mirror the legacy `related` slug list into memory_links as relates_to edges.

    Additive and idempotent, like `links`. Slugs naming no memory (or the memory
    itself) are skipped, never guessed at, and returned so the caller can report them.
    """
    slugs = [x for x in (t.strip() for t in (related or "").split(",")) if x]
    existing = {r[0] for r in con.execute(
        f"SELECT name FROM memories WHERE name IN ({','.join('?' * len(slugs))})", slugs
    )} if slugs else set()
    _write_links(con, name, [(x, "relates_to") for x in slugs if x in existing and x != name])
    return [x for x in slugs if x not in existing or x == name]


def _links_for(con: sqlite3.Connection, memory_id: int) -> list[dict]:
    """Typed links touching a memory. Symmetric edges appear from either end with direction 'both'."""
    rows = con.execute(
        """
        SELECT l.type, e.symmetric, l.src_id = ? AS outgoing, m.name
        FROM memory_links l
        JOIN edge_types e ON e.type = l.type
        JOIN memories m ON m.id = CASE WHEN l.src_id = ? THEN l.dst_id ELSE l.src_id END
        WHERE l.src_id = ? OR l.dst_id = ?
        ORDER BY l.type, m.name
        """,
        (memory_id,) * 4,
    ).fetchall()
    return [
        {"type": t, "name": n, "direction": "both" if sym else ("out" if out else "in")}
        for t, sym, out, n in rows
    ]


def handle_add(
    name: str,
    type: str,
    body: str,
    tags: str = "",
    files: str = "",
    docs: str = "",
    related: str = "",
    links: list | None = None,
) -> dict:
    """Insert or update a memory in MEMORY.sqlite.

    Args:
        name:   Unique slug (kebab-case). Existing entry is overwritten.
        type:   One of: user, feedback, project, reference.
        body:   Memory content. For feedback/project include Why: and How to apply: lines.
        tags:   Comma-separated keywords for retrieval scoring.
        files:  Comma-separated source file paths this memory relates to.
        docs:   Comma-separated vault doc paths linked to this memory.
        related: Comma-separated memory slugs. Kept in the column and also mirrored into
                memory_links as relates_to edges (additive); slugs naming no memory are
                skipped and returned as `unresolved_related`.
        links:  Typed links to EXISTING memories: [{"to": slug, "type": t}], t one of
                relates_to (default), supersedes, contradicts, learned_from, extends.
                A bare slug string means relates_to. Additive: re-adding never removes
                edges. Any invalid link rejects the whole call (nothing is written).
    """
    if type not in VALID_TYPES:
        _log.warning("handle_add rejected invalid type '%s' for name='%s'", type, name)
        return {"error": f"Invalid type '{type}'. Must be one of: {', '.join(VALID_TYPES)}"}

    parsed, err = _normalize_links(links)
    if err:
        return {"error": err}

    with _connect() as con:
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        _ensure_links_schema(con)
        err = _validate_links(con, name, parsed)
        if err:
            _log.warning("handle_add rejected links for name='%s': %s", name, err)
            return {"error": err}
        con.execute(
            """
            INSERT INTO memories (name, type, tags, body, files, docs, related, updated, last_validated)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
                type=excluded.type,
                tags=excluded.tags,
                body=excluded.body,
                files=excluded.files,
                docs=excluded.docs,
                related=excluded.related,
                updated=excluded.updated,
                last_validated=excluded.last_validated
            """,
            (name, type, tags, body, files, docs, related),
        )
        _write_links(con, name, parsed)
        unresolved = _write_related(con, name, related)
    if unresolved:
        _log.warning("memory '%s': related slugs not mirrored (no such memory): %s", name, unresolved)
    _log.info("memory upserted: name='%s' type=%s links=%d", name, type, len(parsed))
    try:
        from scripts.build_memories_embeddings import upsert_memories
        upsert_memories([name])
    except Exception as exc:
        _log.warning("memory vector upsert failed for '%s': %s", name, exc)
    result = {"ok": True, "name": name, "action": "upserted"}
    if unresolved:
        result["unresolved_related"] = unresolved
    return result


def handle_add_batch(memories: list[dict]) -> dict:
    """Insert or update multiple memories in one call.

    Args:
        memories: List of memory dicts, each with keys: name, type, body,
                  tags (optional, default ''), links (optional; see memory__add,
                  targets may be other memories in the same batch).
    """
    results = []
    names_to_embed = []
    batch_names = {m.get("name", "") for m in memories}
    pending_links: list[tuple[str, list[tuple[str, str]]]] = []
    pending_related: list[tuple[str, str]] = []
    with _connect() as con:
        _ensure_schema(con)
        _ensure_links_schema(con)
        for m in memories:
            name   = m.get("name", "")
            mtype  = m.get("type", "")
            body   = m.get("body", "")
            tags   = m.get("tags", "")
            files   = m.get("files", "")
            docs    = m.get("docs", "")
            related = m.get("related", "")
            if not name or not mtype or not body:
                results.append({"name": name, "error": "missing required field (name/type/body)"})
                continue
            if mtype not in VALID_TYPES:
                results.append({"name": name, "error": f"invalid type '{mtype}'"})
                continue
            parsed, err = _normalize_links(m.get("links"))
            err = err or _validate_links(con, name, parsed, batch_names)
            if err:
                results.append({"name": name, "error": err})
                continue
            con.execute(
                """
                INSERT INTO memories (name, type, tags, body, files, docs, related, updated, last_validated)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    type=excluded.type,
                    tags=excluded.tags,
                    body=excluded.body,
                    files=excluded.files,
                    docs=excluded.docs,
                    related=excluded.related,
                    updated=excluded.updated,
                    last_validated=excluded.last_validated
                """,
                (name, mtype, tags, body, files, docs, related),
            )
            pending_links.append((name, parsed))
            pending_related.append((name, related))
            results.append({"name": name, "action": "upserted"})
            names_to_embed.append(name)
            _log.info("memory upserted: name='%s' type=%s", name, mtype)

        # After all upserts, so a link may target a memory added earlier in the same batch.
        # A target that was itself rejected in this batch fails here, not silently.
        for name, parsed in pending_links:
            try:
                _write_links(con, name, parsed)
            except (sqlite3.DatabaseError, TypeError) as exc:
                for r in results:
                    if r["name"] == name:
                        r.pop("action", None)
                        r["error"] = f"link write failed: {exc} (memory saved without links)"
                        r["action"] = "upserted"

        for name, related in pending_related:
            unresolved = _write_related(con, name, related)
            if unresolved:
                for r in results:
                    if r["name"] == name:
                        r["unresolved_related"] = unresolved

    if names_to_embed:
        try:
            from scripts.build_memories_embeddings import upsert_memories
            upsert_memories(names_to_embed)
        except Exception as exc:
            _log.warning("memory vector upsert failed for batch: %s", exc)

    ok_count = sum(1 for r in results if "action" in r)
    return {"ok": True, "count": ok_count, "results": results}


def _normalize_slug(s: str) -> str:
    """Strip hyphens and underscores for slug-insensitive comparison."""
    return s.replace("-", "").replace("_", "")


def _search_rows(query: str, type: str, con: sqlite3.Connection) -> list:
    like = f"%{query}%"
    norm = f"%{_normalize_slug(query)}%"
    # REPLACE(name,'-','') and REPLACE(...,'_','') lets SQLite compare normalized slugs
    sql = (
        "SELECT id, name, type, tags, body, updated FROM memories "
        "WHERE (name LIKE ? OR REPLACE(REPLACE(name,'-',''),'_','') LIKE ? "
        "OR tags LIKE ? OR body LIKE ?)"
    )
    params: list = [like, norm, like, like]
    if type:
        sql += " AND type = ?"
        params.append(type)
    sql += " ORDER BY updated DESC LIMIT 20"
    return con.execute(sql, params).fetchall()


def handle_search(query: str, type: str = "") -> dict:
    """Search memories by keyword across name, tags, and body.

    Slug normalization: underscores and hyphens are stripped before matching
    the name column, so 'claude_hooks_goals' matches 'claude-hooks-goals'.

    If the full query returns no results and contains multiple words, retries
    with each individual keyword and unions the results.

    Args:
        query:  Keyword(s) to search for (case-insensitive).
        type:   Optional filter by type (user/feedback/project/reference).
    """
    with sqlite3.connect(MEMORY_DB) as con:
        con.row_factory = sqlite3.Row
        rows = _search_rows(query, type, con)

        if not rows:
            # Split on whitespace AND on slug separators so 'claude_hooks' → ['claude', 'hooks']
            raw_tokens = query.replace("_", " ").replace("-", " ").split()
            tokens = [t for t in raw_tokens if len(t) > 2]
            if len(tokens) > 1:
                seen: dict[int, sqlite3.Row] = {}
                for token in tokens:
                    for row in _search_rows(token, type, con):
                        seen.setdefault(row["id"], row)
                rows = sorted(seen.values(), key=lambda r: r["updated"], reverse=True)

    results = [dict(r) for r in rows]
    if results:
        with sqlite3.connect(MEMORY_DB) as con:
            try:
                for r in results:
                    r["links"] = _links_for(con, r["id"])
            except sqlite3.OperationalError:
                pass  # links table not created yet (no memory added since the feature shipped)

    _log.debug("handle_search query='%s' type=%s → %d results", query, type, len(results))
    return {
        "count": len(results),
        "results": results,
    }


def handle_list(type: str = "") -> dict:
    """List all memories, optionally filtered by type.

    Args:
        type:   Optional filter by type (user/feedback/project/reference).
    """
    sql = "SELECT id, name, type, tags, updated FROM memories WHERE 1=1"
    params: list = []

    if type:
        sql += " AND type = ?"
        params.append(type)

    sql += " ORDER BY updated DESC"

    with sqlite3.connect(MEMORY_DB) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()

    return {
        "count": len(rows),
        "memories": [dict(r) for r in rows],
    }


def handle_get(name: str) -> dict:
    """Get the full body of a single memory by name.

    Args:
        name: The memory slug (exact match).
    """
    with sqlite3.connect(MEMORY_DB) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM memories WHERE name = ?", (name,)
        ).fetchone()

    if not row:
        _log.warning("handle_get: no memory found with name='%s'", name)
        return {"error": f"No memory found with name '{name}'"}
    _log.debug("handle_get: fetched name='%s'", name)
    result = dict(row)
    with sqlite3.connect(MEMORY_DB) as con:
        try:
            result["links"] = _links_for(con, result["id"])
        except sqlite3.OperationalError:
            result["links"] = []
    return result


def handle_tool_hints(top_n: int = 20) -> dict:
    """Show MCP tool usage stats from tool_hints.sqlite.

    Args:
        top_n:  Max rows to return, sorted by count descending.
    """
    if not TOOL_HINTS_DB.exists():
        return {"error": "tool_hints.sqlite not found — no tool calls logged yet."}

    sql = "SELECT tool_name, count, last_used, avg_latency_ms, keywords, skill FROM mcp_tool_hints"
    params: list = []

    sql += " ORDER BY count DESC LIMIT ?"
    params.append(top_n)

    with sqlite3.connect(TOOL_HINTS_DB) as con:
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return {"error": "mcp_tool_hints table not found — no tool calls logged yet."}

    return {
        "count": len(rows),
        "tools": [dict(r) for r in rows],
    }




def handle_read_compact(session_id: str) -> dict:
    """Read the compact summary written by /compact for a given session.

    Scans the session's JSONL file for the last continuation-summary message
    injected by Claude Code's /compact command.

    Args:
        session_id: The Claude Code session ID (UUID).
    """
    projects_root = Path.home() / ".claude" / "projects"

    # Find the JSONL — it lives under <projects_root>/<slug>/<session_id>.jsonl
    matches = list(projects_root.rglob(f"{session_id}.jsonl"))
    if not matches:
        return {"error": f"No JSONL found for session_id '{session_id}'"}

    jsonl_path = matches[0]
    summary_text: str | None = None

    _COMPACT_MARKER = "This session is being continued from a previous conversation"

    with jsonl_path.open() as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "user":
                continue
            content = obj.get("message", {}).get("content", "")
            if isinstance(content, str) and content.startswith(_COMPACT_MARKER):
                summary_text = content  # keep last occurrence

    if summary_text is None:
        return {"error": "No compact summary found in this session's JSONL"}

    # Extract just the Summary block (between "Summary:" and the next top-level section)
    m = re.search(r"^Summary:\n(.*?)(?=\nIf you need specific details|\Z)", summary_text, re.DOTALL | re.MULTILINE)
    summary_body = m.group(1).strip() if m else summary_text

    return {
        "session_id": session_id,
        "jsonl_path": str(jsonl_path),
        "summary": summary_body,
    }


def handle_stale(
    unhit_days: int = 30,
    unvalidated_days: int = 30,
    type: str = "",
    limit: int = 20,
) -> dict:
    """List memories due for review: not retrieved recently AND not validated recently.

    A memory is stale when it has never been hit or its last_hit is older than
    unhit_days, and its last_validated (falling back to updated) is older than
    unvalidated_days or unset. Never-hit memories come first, then oldest last_hit.
    Review each one: keep → memory__validate, fix → memory__add, wrong → memory__delete.

    Args:
        unhit_days:       Retrieval-age threshold in days.
        unvalidated_days: Validation-age threshold in days.
        type:             Optional filter by type (user/feedback/project/reference).
        limit:            Max rows to return (one review batch).
    """
    where = """
        (last_hit IS NULL OR last_hit < datetime('now', ?))
        AND (COALESCE(last_validated, updated) IS NULL
             OR COALESCE(last_validated, updated) < datetime('now', ?))
    """
    params: list = [f"-{int(unhit_days)} days", f"-{int(unvalidated_days)} days"]
    if type:
        where += " AND type = ?"
        params.append(type)

    with sqlite3.connect(MEMORY_DB) as con:
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        total = con.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params).fetchone()[0]
        rows = con.execute(
            f"""
            SELECT name, type, tags, substr(body, 1, 300) AS body_preview,
                   COALESCE(hit_count, 0) AS hit_count, last_hit, last_validated, updated
            FROM memories WHERE {where}
            ORDER BY (last_hit IS NOT NULL), last_hit, COALESCE(last_validated, updated)
            LIMIT ?
            """,
            params + [int(limit)],
        ).fetchall()

    return {"total_stale": total, "count": len(rows), "memories": [dict(r) for r in rows]}


def handle_validate(names: list[str]) -> dict:
    """Mark memories as reviewed and still correct (sets last_validated to now).

    Args:
        names: Memory slugs to re-validate.
    """
    validated, missing = [], []
    with sqlite3.connect(MEMORY_DB) as con:
        _ensure_schema(con)
        for name in names:
            cur = con.execute(
                "UPDATE memories SET last_validated = datetime('now') WHERE name = ?", (name,)
            )
            (validated if cur.rowcount else missing).append(name)
    _log.info("memories validated: %s (missing: %s)", validated, missing)
    result: dict = {"ok": True, "validated": validated}
    if missing:
        result["missing"] = missing
    return result


def handle_delete(name: str) -> dict:
    """Delete a memory by name.

    Args:
        name: The memory slug to delete.
    """
    with _connect() as con:
        con.row_factory = sqlite3.Row
        # The memory_links_cascade trigger removes its edges, FK pragma or not.
        cur = con.execute("DELETE FROM memories WHERE name = ?", (name,))

    if cur.rowcount == 0:
        _log.warning("handle_delete: no memory found with name='%s'", name)
        return {"error": f"No memory found with name '{name}'"}
    _log.info("memory deleted: name='%s'", name)
    try:
        from scripts.build_memories_embeddings import remove_memory
        remove_memory(name)
    except Exception as exc:
        _log.warning("memory vector remove failed for '%s': %s", name, exc)
    return {"ok": True, "deleted": name}
