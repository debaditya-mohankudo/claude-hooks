"""Derived SQLite index over the Obsidian vault: files, tags, links, sections, FTS5.

The markdown notes stay the source of truth; this database is a cache that can be
deleted and rebuilt at any time. `reindex_all` compares each note's mtime against
the stored one and re-parses only what changed, so it is cheap to call after a
graph run or on server start. `reindex_file` is the single-note path that a
PostToolUse hook on vault writes can call.

Sections are the unit the old VAULT_MAP.json lacked: every heading becomes a row
with its line range and, when the heading carries a date ("run 2026-10-17"), an
`entry_date`, so a dated run-log entry inside a long archive note is findable.

Set VAULT_INDEX_DB / VAULT_ROOT to point elsewhere; defaults are
~/.claude/vault_index.sqlite and the iCloud Obsidian vault.
"""
from __future__ import annotations

import os
import re
import shlex
import sqlite3
from pathlib import Path

VAULT = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents"
DB = Path.home() / ".claude/vault_index.sqlite"
SKIP_DIRS = {".obsidian", ".git", ".trash", "Tmp", "__pycache__"}
SKIP_FILES = {"VAULT_MAP.md"}
SCHEMA_VERSION = "2"
STALE_AFTER_S = 60  # reindex_if_stale: a full mtime-gated pass at most this often

# MCP vault tools that change notes, and the argument names that carry a note path.
# `mac vault` verbs that change notes, whether run through Bash or the MCP vault tools
# (hooks/core/mac_command.py maps both to these ids; task:0aa96180).
WRITE_COMMANDS = {"mac vault append", "mac vault write", "mac vault move", "mac vault delete"}
PATH_ARGS = ("path", "to")
# Claude Code's own file-editing tools (arg `file_path`); a Bash command is scanned for note paths instead.
EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}
MAX_BASH_PATHS = 20
STOP = set("the a an of and or to in for on with is are md note notes untitled class classes".split())

WIKI = re.compile(r"\[\[([^\]|#]+)")
INLINE_TAG = re.compile(r"(?:^|\s)#([A-Za-z][\w/-]+)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
FENCE = re.compile(r"^\s*(```|~~~)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, stem TEXT NOT NULL, title TEXT, mtime REAL NOT NULL,
  bytes INTEGER, unreadable INTEGER DEFAULT 0, indexed_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS files_stem ON files(stem);
CREATE TABLE IF NOT EXISTS tags (
  path TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (path, tag));
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
CREATE TABLE IF NOT EXISTS links (
  src_path TEXT NOT NULL, dst_stem TEXT NOT NULL, PRIMARY KEY (src_path, dst_stem));
CREATE INDEX IF NOT EXISTS links_dst ON links(dst_stem);
CREATE TABLE IF NOT EXISTS props (
  path TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (path, key));
CREATE TABLE IF NOT EXISTS sections (
  id INTEGER PRIMARY KEY, path TEXT NOT NULL, level INTEGER NOT NULL, heading TEXT,
  line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, entry_date TEXT);
CREATE INDEX IF NOT EXISTS sections_path ON sections(path);
CREATE INDEX IF NOT EXISTS sections_date ON sections(entry_date);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  heading, body, tokenize='porter unicode61');  -- rowid = sections.id
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIEW IF NOT EXISTS links_resolved AS
  SELECT l.src_path, f.path AS dst_path FROM links l JOIN files f ON f.stem = l.dst_stem
  WHERE f.path != l.src_path;
"""


def connect(db: Path | str | None = None) -> sqlite3.Connection:
    """Open the index, rebuilding it from scratch if its schema version is not this code's."""
    p = Path(db or os.environ.get("VAULT_INDEX_DB") or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    try:
        have = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        have = None
    if have and have[0] != SCHEMA_VERSION:  # derived cache: drop and let reindex_all refill it
        for kind, name in conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall():
            if kind in ("table", "view") and not name.startswith("fts_"):
                conn.execute(f"DROP {kind.upper()} IF EXISTS {name}")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def vault_root(root: Path | str | None = None) -> Path:
    return Path(root or os.environ.get("VAULT_ROOT") or VAULT)


def words(s: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z]{3,}", s.lower()) if w not in STOP]


def _split_frontmatter(lines: list[str]) -> tuple[dict[str, str], list[str], int]:
    """Return (props, frontmatter tags, index of the first body line)."""
    if not lines or lines[0].strip() != "---":
        return {}, [], 0
    for i in range(1, min(len(lines), 60)):
        if lines[i].strip() == "---":
            props: dict[str, str] = {}
            tags: list[str] = []
            key = None
            for ln in lines[1:i]:
                m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", ln)
                if m:
                    key, val = m.group(1), m.group(2).strip()
                    if key == "tags":
                        tags += [t.strip(" '\"#") for t in val.strip("[]").split(",") if t.strip(" '\"#[]")]
                    else:
                        props[key] = val
                elif key == "tags" and ln.strip().startswith("-"):
                    tags.append(ln.strip()[1:].strip(" '\"#"))
            return props, [t for t in tags if t], i + 1
    return {}, [], 0


def parse_note(text: str) -> dict:
    """Parse one note into title, tags, wikilink stems, props and sections."""
    lines = text.split("\n")
    props, fm_tags, body_start = _split_frontmatter(lines)
    sections: list[dict] = []
    cur = {"level": 0, "heading": "", "start": body_start + 1, "lines": []}
    tags = set(fm_tags)
    links: set[str] = set()
    in_fence = False
    for n, ln in enumerate(lines[body_start:], start=body_start + 1):
        if FENCE.match(ln):
            in_fence = not in_fence
            cur["lines"].append(ln)
            continue
        if not in_fence:
            h = HEADING.match(ln)
            if h:
                sections.append(cur)
                cur = {"level": len(h.group(1)), "heading": h.group(2).strip(), "start": n, "lines": [ln]}
                continue
            tags.update(INLINE_TAG.findall(ln))
            links.update(w.strip() for w in WIKI.findall(ln))
        cur["lines"].append(ln)
    sections.append(cur)
    out = []
    for s in sections:
        body = "\n".join(s["lines"]).strip()
        if s["level"] == 0 and not body:
            continue
        d = DATE.search(s["heading"])
        out.append({"level": s["level"], "heading": s["heading"], "line_start": s["start"],
                    "line_end": s["start"] + max(len(s["lines"]) - 1, 0),
                    "entry_date": d.group(1) if d else None, "body": body})
    first = next((s["heading"] for s in out if s["level"] >= 1), "")
    return {"title": first, "tags": sorted(tags), "links": sorted(links), "props": props, "sections": out}


def _drop(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM sections WHERE path = ?)", (path,))
    for t in ("files", "tags", "props", "sections"):
        conn.execute(f"DELETE FROM {t} WHERE path = ?", (path,))
    conn.execute("DELETE FROM links WHERE src_path = ?", (path,))


def reindex_file(conn: sqlite3.Connection, root: Path | str | None, rel: str) -> str:
    """(Re)index one note by vault-relative path. Returns 'indexed', 'removed' or 'unreadable'."""
    p = vault_root(root) / rel
    _drop(conn, rel)
    try:
        st = p.stat()
        text = p.read_text(errors="ignore")
    except OSError:
        # Missing -> removed. A present-but-unreadable iCloud placeholder keeps a stub row.
        if p.exists():
            conn.execute("INSERT INTO files (path, stem, title, mtime, bytes, unreadable) VALUES (?,?,?,?,0,1)",
                         (rel, p.stem, p.stem, 0.0))
            return "unreadable"
        return "removed"
    note = parse_note(text)
    conn.execute("INSERT INTO files (path, stem, title, mtime, bytes) VALUES (?,?,?,?,?)",
                 (rel, p.stem, note["title"] or p.stem, st.st_mtime, len(text)))
    conn.executemany("INSERT INTO tags VALUES (?,?)", [(rel, t) for t in note["tags"]])
    conn.executemany("INSERT INTO links VALUES (?,?)", [(rel, w) for w in note["links"]])
    conn.executemany("INSERT INTO props VALUES (?,?,?)", [(rel, k, v) for k, v in note["props"].items()])
    for s in note["sections"]:
        cur = conn.execute(
            "INSERT INTO sections (path, level, heading, line_start, line_end, entry_date) VALUES (?,?,?,?,?,?)",
            (rel, s["level"], s["heading"], s["line_start"], s["line_end"], s["entry_date"]))
        conn.execute("INSERT INTO fts (rowid, heading, body) VALUES (?,?,?)",
                     (cur.lastrowid, s["heading"], s["body"]))
    return "indexed"


def _walk(root: Path):
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]) or rel.name in SKIP_FILES:
            continue
        yield str(rel)


def reindex_all(conn: sqlite3.Connection, root: Path | str | None = None, force: bool = False) -> dict:
    """Bring the index in line with the vault: new/changed notes re-parsed, deleted notes dropped."""
    r = vault_root(root)
    stored = {path: (mtime, unreadable) for path, mtime, unreadable in
              conn.execute("SELECT path, mtime, unreadable FROM files")}
    stats = {"indexed": 0, "unchanged": 0, "removed": 0, "unreadable": 0}
    seen: set[str] = set()
    for rel in _walk(r):
        seen.add(rel)
        try:
            mtime = (r / rel).stat().st_mtime
        except OSError:
            mtime = None
        prev = stored.get(rel)
        if not force and prev and mtime is not None and prev[0] == mtime and not prev[1]:
            stats["unchanged"] += 1
            continue
        stats[reindex_file(conn, r, rel)] += 1
    for gone in set(stored) - seen:
        _drop(conn, gone)
        stats["removed"] += 1
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_full_build', datetime('now'))")
    conn.commit()
    return stats


def reindex_if_stale(conn: sqlite3.Connection, ttl_s: int = STALE_AFTER_S) -> bool:
    """Run reindex_all unless one finished within ttl_s. Catches edits no tool hook sees (Obsidian)."""
    row = conn.execute("SELECT CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', value) AS INTEGER) "
                       "FROM meta WHERE key = 'last_full_build'").fetchone()
    if row and row[0] is not None and row[0] < ttl_s:
        return False
    reindex_all(conn)
    return True


def _note_paths(tool_input: dict, root: Path) -> list[str]:
    """Vault-relative .md paths named in a vault tool's input; absolute paths under root are relativised."""
    out: list[str] = []
    for key in PATH_ARGS:
        val = tool_input.get(key)
        if not isinstance(val, str) or not val.endswith(".md"):
            continue
        try:
            rel = str(Path(val).relative_to(root)) if Path(val).is_absolute() else val.lstrip("/")
        except ValueError:
            continue
        if rel not in out:
            out.append(rel)
    return out


def reindex_for_command(command_id: str, tool_input: dict) -> str | None:
    """PostToolUse entry point: reindex the notes a vault write command touched.

    tool_input carries `path` (and `to` for a move), as the MCP tools take them.

    Returns a one-line result, or None for any other tool. Never raises -- a failure
    is stored in meta.last_error so a stale index is visible, not silent. A call that
    names no .md path is left to reindex_if_stale rather than walking the vault inline.
    """
    if command_id not in WRITE_COMMANDS:
        return None
    try:
        conn = connect()
        try:
            rels = _note_paths(tool_input if isinstance(tool_input, dict) else {}, vault_root())
            result = ", ".join(f"{r}={reindex_file(conn, vault_root(), r)}" for r in rels) or "no .md path named"
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_error', '')")
            conn.commit()
        finally:
            conn.close()
        return result
    except Exception as exc:
        _record_error(command_id, exc)
        return None


def _record_error(label: str, exc: Exception) -> None:
    """Store a reindex failure in meta.last_error so a stale index is visible, never raise."""
    try:
        conn = connect()
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_error', datetime('now') || ' ' || ? || ': ' || ?)",
                     (label, str(exc)))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _indexable(rel: str) -> bool:
    parts = Path(rel).parts
    return (rel.endswith(".md") and parts[-1] not in SKIP_FILES
            and not any(x in SKIP_DIRS or x.startswith(".") for x in parts[:-1]))


def _rel_in_vault(raw: str, root: Path, cwd: str) -> str | None:
    """A path word as a vault-relative note path, or None when it is not under the vault.

    Absolute and ~ paths stand alone; a relative one is tried against the call's cwd, then
    the vault root. The first candidate that is an existing file wins; failing that, the
    first one that lies under the vault (a deleted note still needs dropping from the index).
    """
    p = Path(raw).expanduser()
    bases = [None] if p.is_absolute() else [Path(cwd) if cwd else None, root]
    first = None
    for base in bases:
        cand = p if base is None else base / p
        try:
            rel = str(Path(os.path.normpath(cand)).relative_to(root))
        except ValueError:
            continue
        if not _indexable(rel):
            continue
        if (root / rel).is_file():
            return rel
        first = first or rel
    return first


def _bash_note_paths(command: str, root: Path, cwd: str) -> list[str]:
    """Vault note paths named in a shell command (`sed -i ... "Market/x.md"`, `f="a/b.md"`, `>> a/b.md`)."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    out: list[str] = []
    for w in words:
        w = w.split("=")[-1].lstrip("<>")  # f="a/b.md" and >>a/b.md
        if not w.endswith(".md"):
            continue
        rel = _rel_in_vault(w, root, cwd)
        if rel and rel not in out:
            out.append(rel)
    return out[:MAX_BASH_PATHS]


def reindex_for_tool(tool_name: str, tool_input: dict, cwd: str = "") -> str | None:
    """PostToolUse entry point for edits `reindex_for_command` cannot see.

    Edit/Write/MultiEdit name the note in `file_path`; a Bash command is scanned for .md words
    that resolve under the vault (sed -i, redirects, python one-liners naming the file). Only a
    note whose mtime differs from the indexed one, or that is gone, is re-parsed, so a command
    that merely reads a note costs one stat. Edits made inside scripts, by Obsidian or from
    another device are left to reindex_if_stale and the vault_log scan.

    Returns a one-line result, or None when nothing under the vault was named. Never raises.
    """
    try:
        root = vault_root()
        if tool_name in EDIT_TOOLS:
            fp = tool_input.get("file_path") if isinstance(tool_input, dict) else None
            rel = _rel_in_vault(fp, root, cwd) if isinstance(fp, str) and fp.endswith(".md") else None
            rels = [rel] if rel else []
        elif tool_name == "Bash" and isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
            rels = _bash_note_paths(tool_input["command"], root, cwd)
        else:
            return None
        if not rels:
            return None
        conn = connect()
        try:
            done = []
            for rel in rels:
                try:
                    mtime = (root / rel).stat().st_mtime
                except OSError:
                    mtime = None
                row = conn.execute("SELECT mtime, unreadable FROM files WHERE path = ?", (rel,)).fetchone()
                if row is None and mtime is None:
                    continue  # named but never existed and never indexed
                if row and mtime is not None and row[0] == mtime and not row[1]:
                    continue  # already current
                done.append(f"{rel}={reindex_file(conn, root, rel)}")
            if done:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_error', '')")
                conn.commit()
        finally:
            conn.close()
        return ", ".join(done) or None
    except Exception as exc:
        _record_error(tool_name, exc)
        return None


def search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """FTS5 search; returns note path, section heading, line range and entry_date."""
    rows = conn.execute(
        "SELECT s.path, s.heading, s.line_start, s.line_end, s.entry_date FROM fts "
        "JOIN sections s ON s.id = fts.rowid WHERE fts MATCH ? ORDER BY rank LIMIT ?",
        (query, limit)).fetchall()
    return [dict(zip(("path", "heading", "line_start", "line_end", "entry_date"), r)) for r in rows]


def main() -> None:
    import argparse
    import time
    ap = argparse.ArgumentParser(description="Build or refresh the vault SQLite index")
    ap.add_argument("--force", action="store_true", help="re-parse every note")
    ap.add_argument("--search", help="run an FTS5 query after indexing")
    a = ap.parse_args()
    t0 = time.time()
    conn = connect()
    print(reindex_all(conn, force=a.force), f"{time.time() - t0:.1f}s")
    if a.search:
        for h in search(conn, a.search):
            print(h)


if __name__ == "__main__":
    main()
