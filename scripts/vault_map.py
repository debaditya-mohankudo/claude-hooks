#!/usr/bin/env python3
"""Broad map of the Obsidian vault: which dir / file holds what.

Writes <vault>/VAULT_MAP.json (graph: dir/file nodes, `contains` + `links_to`
edges) and <vault>/VAULT_MAP.md (readable tree, one line per dir).
Mechanical only: titles, headings, tags, wikilinks. No LLM. Dated run files
(2026-10-16.md, ..._2026-W14.md) are summarised once per dir as a series, not
described individually in the dir's keywords, tags or samples.

Usage: uv run python scripts/vault_map.py [--vault PATH] [--depth N]
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.tools.vault_map import _iso, _series_key, _stamp  # dated-series detection shared with the MCP tools

VAULT = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents"
SKIP_DIRS = {".obsidian", ".git", ".trash", "Tmp", "__pycache__"}
WIKI = re.compile(r"\[\[([^\]|#]+)")
TAG = re.compile(r"(?:^|\s)#([A-Za-z][\w/-]+)")
FM_TAGS = re.compile(r"^tags:\s*\[?([^\]\n]*)\]?", re.M)
HEAD = re.compile(r"^#{1,3}\s+(.+)$", re.M)
STOP = set("the a an of and or to in for on with is are md note notes untitled class classes".split())


def scan_file(p: Path, root: Path) -> dict:
    try:
        text = p.read_text(errors="ignore")
    except OSError:  # iCloud placeholder not downloaded / timeout
        return {"id": str(p.relative_to(root)), "type": "file", "title": p.stem, "headings": [],
                "tags": [], "links": [], "bytes": 0, "unreadable": True}
    heads = [h.strip() for h in HEAD.findall(text)][:8]
    tags = set(TAG.findall(text))
    m = FM_TAGS.search(text)
    if m:
        tags |= {t.strip(" '\"#") for t in m.group(1).split(",") if t.strip()}
    return {
        "id": str(p.relative_to(root)),
        "type": "file",
        "title": heads[0] if heads else p.stem,
        "headings": heads[1:6],
        "tags": sorted(tags)[:10],
        "links": sorted({w.strip() for w in WIKI.findall(text)}),
        "bytes": len(text),
    }


def words(s: str):
    return [w for w in re.findall(r"[A-Za-z]{3,}", s.lower()) if w not in STOP]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", type=Path, default=VAULT)
    ap.add_argument("--depth", type=int, default=2, help="max dir depth listed in the .md tree")
    a = ap.parse_args()
    root = a.vault

    files = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if rel.name in ("VAULT_MAP.md",):
            continue
        files.append(scan_file(p, root))

    stem_to_id = {}
    for f in files:
        stem_to_id.setdefault(Path(f["id"]).stem, f["id"])

    dirs: dict[str, dict] = {}
    series: dict[str, dict[str, list[str]]] = {}  # dir -> series pattern -> dated stems
    for f in files:
        parts = Path(f["id"]).parts[:-1]
        for i in range(len(parts) + 1):
            d = "/".join(parts[:i]) or "."
            e = dirs.setdefault(d, {"id": d, "type": "dir", "files": 0, "tags": Counter(),
                                    "words": Counter(), "samples": [], "children": set()})
            e["files"] += 1
            if i < len(parts):
                e["children"].add("/".join(parts[:i + 1]))
            key = _series_key(f["id"])
            if key:
                if i == len(parts):
                    series.setdefault(d, {}).setdefault(key, []).append(Path(f["id"]).stem)
                continue
            e["tags"].update(f["tags"])
            e["words"].update(words(f["title"] + " " + " ".join(f["headings"])))
            if i == len(parts) and len(e["samples"]) < 5:
                e["samples"].append(Path(f["id"]).name)

    nodes, edges = [], []
    for d in sorted(dirs):
        e = dirs[d]
        nodes.append({"id": d, "type": "dir", "files": e["files"],
                      "top_tags": [t for t, _ in e["tags"].most_common(6)],
                      "top_words": [w for w, _ in e["words"].most_common(8)],
                      "samples": e["samples"],
                      "series": [{"pattern": k, "count": len(v), "first": min(map(_stamp, v), key=_iso),
                                  "last": max(map(_stamp, v), key=_iso)}
                                 for k, v in sorted(series.get(d, {}).items())]})
        for c in sorted(e["children"]):
            edges.append({"from": d, "to": c, "rel": "contains"})
    nodes_by_id = {n["id"]: n for n in nodes}
    for f in files:
        d = "/".join(Path(f["id"]).parts[:-1]) or "."
        links = [stem_to_id[l] for l in f.pop("links") if l in stem_to_id]
        nodes.append(f)
        edges.append({"from": d, "to": f["id"], "rel": "contains"})
        edges += [{"from": f["id"], "to": t, "rel": "links_to"} for t in links if t != f["id"]]

    tag_files: dict[str, list[str]] = {}
    for f in files:
        for t in f["tags"]:
            tag_files.setdefault(t, []).append(f["id"])
    for t, ids in sorted(tag_files.items()):
        nodes.append({"id": f"tag:{t}", "type": "tag", "files": len(ids)})
        edges += [{"from": i, "to": f"tag:{t}", "rel": "tagged"} for i in ids]

    (root / "VAULT_MAP.json").write_text(json.dumps({"nodes": nodes, "edges": edges}, indent=1))

    lines = ["# Vault map", "",
             f"{len(files)} notes. Generated by `vault_map.py`; full graph in `VAULT_MAP.json`.", ""]
    for d in sorted(dirs):
        depth = 0 if d == "." else d.count("/") + 1
        if d == "." or depth > a.depth:
            continue
        e = dirs[d]
        tags = ", ".join(t for t, _ in e["tags"].most_common(5))
        kw = ", ".join(w for w, _ in e["words"].most_common(8))
        ser = "; ".join(f"{x['pattern']} ×{x['count']} {x['first']}..{x['last']}"
                        for x in nodes_by_id[d]["series"])
        lines.append(f"{'  ' * (depth - 1)}- **{d.split('/')[-1]}/** ({e['files']}) — {kw}"
                     + (f" · tags: {tags}" if tags else "") + (f" · series: {ser}" if ser else ""))
    lines += ["", "## Tags (cross-directory)", ""]
    for t, ids in sorted(tag_files.items(), key=lambda kv: -len(kv[1]))[:40]:
        top = Counter(i.split("/")[0] for i in ids).most_common(4)
        lines.append(f"- #{t} ({len(ids)}) — " + ", ".join(f"{d} {n}" for d, n in top))
    bad = sum(1 for f in files if f.get("unreadable"))
    if bad:
        lines.append(f"\n{bad} notes unreadable (iCloud not downloaded); titles from filename only.")
    (root / "VAULT_MAP.md").write_text("\n".join(lines) + "\n")
    print(f"{len(files)} files, {len(dirs)} dirs, {len(edges)} edges")


if __name__ == "__main__":
    main()
