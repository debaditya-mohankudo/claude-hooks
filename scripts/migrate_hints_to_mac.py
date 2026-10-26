"""Rename tool-hint rows for MCP tools `mac` replaces to their `mac <area> <verb>` id (task:0aa96180).

Hooks now record those calls under the command id (hooks/core/mac_command.py),
so `mail__read` and `mac mail read` would otherwise split one tool's history
across two rows. Each old row is folded into the new one: counts add, latency
is count-weighted, last_used and the recent prompts are the most recent, and
keywords are the union minus `mac`.

Hint rows hold short names with the server stripped, so the server is inferred:
`area__verb` is a local-mac tool, `aq__x`/`panchang__x` an astrology tool, a
bare name a wyckoff-analyzer or sysml-mcp tool. A name is renamed only when
exactly one candidate is a command in cli-graph's graph.

    uv run python scripts/migrate_hints_to_mac.py           # print the plan
    uv run python scripts/migrate_hints_to_mac.py --apply   # back up the DB, then rename
"""
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "hooks"), str(ROOT)]

from core.mac_command import _snake, known_ids  # noqa: E402
from langchain_learning.config import config  # noqa: E402

MAX_PROMPTS = 10


def candidates(name: str) -> set[str]:
    out = set()
    if "__" in name:
        area, verb = name.split("__", 1)
        out.add(f"mac {area} {verb}")
    flat = _snake(name.removeprefix("astrology__").replace("__", "_").replace("-", "_"))
    out |= {f"mac astrology {flat}", f"mac wyckoff {flat}", f"mac sysml {flat}"}
    return out


def mac_id(name: str, ids: frozenset[str]) -> str | None:
    if name.startswith("mac "):
        return None
    hits = candidates(name) & ids
    return hits.pop() if len(hits) == 1 else None


def _merge_prompts(*blobs: str) -> str:
    prompts: list = []
    for blob in blobs:
        try:
            prompts += json.loads(blob or "[]")
        except ValueError:
            pass
    return json.dumps(prompts[-MAX_PROMPTS:])


def _merge_keywords(*kws: str) -> str:
    return ",".join(sorted({k for kw in kws for k in (kw or "").split(",") if k and k != "mac"}))


def plan(conn: sqlite3.Connection, ids: frozenset[str]) -> list[tuple[str, str]]:
    names = [r[0] for r in conn.execute("SELECT tool_name FROM mcp_tool_hints")]
    return sorted((n, m) for n in names if (m := mac_id(n, ids)))


def migrate(conn: sqlite3.Connection, renames: list[tuple[str, str]]) -> None:
    cols = "count, last_used, avg_latency_ms, keywords, skill, recent_prompts"
    for old, new in renames:
        o = conn.execute(f"SELECT {cols} FROM mcp_tool_hints WHERE tool_name=?", (old,)).fetchone()
        n = conn.execute(f"SELECT {cols} FROM mcp_tool_hints WHERE tool_name=?", (new,)).fetchone()
        if n is None:
            conn.execute("UPDATE mcp_tool_hints SET tool_name=?, keywords=? WHERE tool_name=?",
                         (new, _merge_keywords(o[3]), old))
            continue
        count = (o[0] or 0) + (n[0] or 0)
        latency = ((o[0] or 0) * (o[2] or 0) + (n[0] or 0) * (n[2] or 0)) / count if count else 0.0
        older, newer = sorted([o, n], key=lambda r: r[1] or "")
        conn.execute(
            "UPDATE mcp_tool_hints SET count=?, last_used=?, avg_latency_ms=?, keywords=?, skill=?, "
            "recent_prompts=?, embedding=NULL WHERE tool_name=?",
            (count, newer[1], round(latency, 2), _merge_keywords(o[3], n[3]), newer[4] or older[4],
             _merge_prompts(older[5], newer[5]), new))
        conn.execute("DELETE FROM mcp_tool_hints WHERE tool_name=?", (old,))


def main(argv):
    ids = known_ids()
    if ids is None:
        raise SystemExit("cli-graph's graph/cli_graph.json is not readable; set CLI_GRAPH_JSON")
    db = config.tool_hints_db
    with sqlite3.connect(db) as conn:
        renames = plan(conn, ids)
    for old, new in renames:
        print(f"{old:40} -> {new}")
    print(f"{len(renames)} row(s) to rename in {db}")
    if "--apply" not in argv or not renames:
        return 0
    backup = db.with_name(f"{db.name}.bak-{datetime.now():%Y-%m-%d}-mac")
    shutil.copy2(db, backup)
    with sqlite3.connect(db) as conn:
        migrate(conn, renames)
    print(f"renamed; backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
