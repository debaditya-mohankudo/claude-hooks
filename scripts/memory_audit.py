"""Review the memory audit log: list recent prompts, mark injected memories, print the report.

  uv run python scripts/memory_audit.py list [--n 10] [--unmarked]
  uv run python scripts/memory_audit.py mark <id> useful=<name>,<name> noise=<name>
  uv run python scripts/memory_audit.py report
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_learning.nodes import _memory_audit as audit  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--n", type=int, default=10)
    ls.add_argument("--unmarked", action="store_true")
    mk = sub.add_parser("mark")
    mk.add_argument("id", type=int)
    mk.add_argument("verdicts", nargs="+", help="useful=a,b  noise=c")
    sub.add_parser("report")
    a = ap.parse_args()

    if a.cmd == "list":
        for r in audit.recent(a.n, unmarked_only=a.unmarked):
            print(f"#{r['id']} {r['prompt'][:70]!r} carried={r['carried']}")
            for c in r["candidates"][:6]:
                tag = "*" if c["name"] in r["injected"] else " "
                print(f"   {tag} {c['score']:>6}  {c['name']}  hits={c['hits']}  {(r['verdict'] or {}).get(c['name'], '')}")
    elif a.cmd == "mark":
        verdicts = {}
        for item in a.verdicts:
            kind, _, names = item.partition("=")
            verdicts.update({n: kind for n in names.split(",") if n})
        audit.mark(a.id, verdicts)
        print(f"marked #{a.id}: {verdicts}")
    else:
        print(json.dumps(audit.report(), indent=2))


if __name__ == "__main__":
    main()
