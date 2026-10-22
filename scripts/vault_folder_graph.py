import json, re
from collections import Counter
from pathlib import Path
root = Path.home()/"Library/Mobile Documents/iCloud~md~obsidian/Documents"
SKIP = {".obsidian",".git",".trash","Tmp","__pycache__"}
files=[]
for p in sorted(root.rglob("*.md")):
    rel=p.relative_to(root)
    if any(x in SKIP or x.startswith(".") for x in rel.parts[:-1]): continue
    files.append(rel)
by_base={}; by_path={}
for r in files:
    by_base.setdefault(r.stem.lower(),[]).append(r)
    by_path[str(r.with_suffix("")).lower()]=r
def top(r): return r.parts[0] if len(r.parts)>1 else "_root"
W=re.compile(r"!?\[\[([^\]|#]+)")
notes=Counter(top(r) for r in files)
internal=Counter(); cross=Counter(); total=0
for r in files:
    try: text=(root/r).read_text(errors="ignore")
    except OSError: continue
    for t in W.findall(text):
        t=t.strip().lower().removesuffix(".md")
        tgt=by_path.get(t)
        if tgt is None:
            c=by_base.get(t.split("/")[-1])
            if not c: continue
            if "/" in t: c=[x for x in c if str(x.with_suffix("")).lower().endswith(t)]
            if not c: continue
            # Obsidian prefers closest path; else first
            tgt=min(c,key=lambda x:(top(x)!=top(r),str(x)))
        total+=1
        a,b=top(r),top(tgt)
        if a==b: internal[a]+=1
        else: cross[tuple(sorted((a,b),key=str.lower))]+=1
names=sorted(notes,key=str.lower)
g={"directed":False,"multigraph":False,"graph":{
 "source":"wikilinks resolved from disk by basename/path (Obsidian rules, md targets only; regenerated without Obsidian running)",
 "vault":"iCloud~md~obsidian/Documents","total_files":len(files),"total_link_instances":total,
 "cross_link_instances":sum(cross.values()),"level":"top-level folder aggregation"},
 "nodes":[{"note_count":notes[n],"internal_links":internal[n],"id":n} for n in names],
 "edges":[{"weight":w,"source":a,"target":b} for (a,b),w in sorted(cross.items(),key=lambda kv:(names.index(kv[0][0]),names.index(kv[0][1])))]}
out=json.dumps(g,indent=2)+"\n"
(root/"LIFE_OS/vault_folder_graph.json").write_text(out)
print(g["graph"]); print({n["id"]:n["note_count"] for n in g["nodes"]})
